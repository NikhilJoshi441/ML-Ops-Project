"""Worker process for both agents.

    python -m triage.agents.worker triage      # service: triage-agent
    python -m triage.agents.worker liability   # service: agent-worker

Each replica: consumes its queue reliably (BLMOVE into a per-worker
processing list), heartbeats, reaps jobs of dead replicas, exposes Prometheus
metrics and a /healthz on WORKER_METRICS_PORT, and records every step in the
run's event log.

Failure semantics (A2): if the triage agent fails, the job is retried
(JOB_MAX_ATTEMPTS) and then the run is closed INCOMPLETE. If the liability
agent dies mid-run, its job is requeued from the persisted TriageBrief, so the
triage work is not repeated. A run that does not complete is returned as an
escalation with `completed=false`, never as an answer.
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from triage.agents import metrics as M
from triage.agents.context import RunContext
from triage.common.http import post_json
from triage.common.logging import agent_name, correlation_id, log, setup_logging
from triage.common.runstore import Q_LIABILITY, Q_TRIAGE, RunStore
from triage.common.telemetry import attached, inject_carrier, setup_tracing, tracer, instrument_httpx
from triage.config import get_settings
from triage.contracts.results import (
    EscalationReason, LiabilityBand, Recommendation, Route, TriageBrief,
)

logger = logging.getLogger("worker")
_stop = threading.Event()


def _serve_metrics(port: int, agent: str, ready: threading.Event) -> None:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/metrics"):
                body, ctype = generate_latest(), CONTENT_TYPE_LATEST
            elif self.path.startswith("/healthz"):
                body, ctype = json.dumps({"status": "ok" if ready.is_set() else "starting", "agent": agent}).encode(), "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def incomplete_result(run_id: str, why: str) -> dict:
    return Recommendation(
        run_id=run_id, completed=False, route=Route.ESCALATE_TO_ANALYST, liability_band=LiabilityBand.UNDETERMINED,
        summary=f"The run did not complete ({why}). No recommendation is made.",
        basis="Incomplete run.", escalation_reasons=[EscalationReason.INCOMPLETE_RUN],
        escalation_path="Refer to a disputes analyst; the case was not analysed.",
    ).model_dump(mode="json")


def finish_run(store: RunStore, http: httpx.Client, run_id: str, result: dict, status: str = "completed") -> None:
    run = store.get(run_id) or {}
    now = time.time()
    store.update(run_id, status=status, stage="done", result=result, completed_at=now)
    e2e = now - float(run.get("created_at", now))
    outcome = "incomplete" if not result.get("completed") else ("escalated" if result["route"] == "ESCALATE_TO_ANALYST" else "recommended")
    M.RUN_E2E.labels(outcome).observe(e2e)
    M.ROUTES.labels(result["route"]).inc()
    for r in result.get("escalation_reasons", []):
        M.ESCALATIONS.labels(r).inc()
    store.event(run_id, "run_completed", "system", status=status, route=result["route"], e2e_latency_s=round(e2e, 3),
                cost_usd=result.get("cost_usd"), tokens=result.get("tokens"))
    cb = (run.get("request") or {}).get("callback_url") if isinstance(run.get("request"), dict) else None
    if cb:
        try:
            post_json(http, cb, {"run_id": run_id, "status": status, "result": result}, timeout_s=5, retries=2)
            store.event(run_id, "callback", "system", url=cb, ok=True)
        except Exception as exc:  # callback is best effort; polling remains available
            store.event(run_id, "callback", "system", url=cb, ok=False, error=str(exc)[:200])


class Worker:
    def __init__(self, kind: str):
        self.kind = kind
        self.agent = "triage-agent" if kind == "triage" else "agent-worker"
        self.queue = Q_TRIAGE if kind == "triage" else Q_LIABILITY
        self.s = get_settings()
        self.store = RunStore(self.s.redis_url, self.s.run_ttl_s)
        self.http = httpx.Client(limits=httpx.Limits(max_connections=20))
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.ready = threading.Event()

    # ------------------------------------------------------------ loop
    def run(self) -> None:
        agent_name.set(self.agent)
        M.WORKER_UP.labels(self.agent).set(time.time())
        threading.Thread(target=_serve_metrics, args=(self.s.worker_metrics_port, self.agent, self.ready), daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        if self.kind == "liability":
            self._warm_up()
        self.ready.set()
        log(logger, logging.INFO, "worker started", worker_id=self.worker_id, queue=self.queue)
        while not _stop.is_set():
            try:
                got = self.store.dequeue(self.queue, self.worker_id, timeout_s=2)
            except Exception as exc:
                log(logger, logging.ERROR, "dequeue failed", error=str(exc))
                time.sleep(1)
                continue
            if got is None:
                continue
            raw, job = got
            M.IN_FLIGHT.labels(self.agent).inc()
            try:
                self.handle(job)
            finally:
                M.IN_FLIGHT.labels(self.agent).dec()
                self.store.ack(self.queue, self.worker_id, raw)
        log(logger, logging.INFO, "worker stopped")

    def _warm_up(self) -> None:
        for _ in range(60):
            try:
                if self.http.get(f"{self.s.retriever_url}/readyz", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)

    def _heartbeat(self) -> None:
        while not _stop.is_set():
            try:
                self.store.heartbeat(self.worker_id)
                if random.random() < 0.3:
                    self.store.reap(self.queue, self.s.job_max_attempts, self._give_up)
                M.QUEUE_DEPTH.labels(self.queue).set(self.store.queue_depth(self.queue))
            except Exception as exc:
                log(logger, logging.WARNING, "heartbeat failed", error=str(exc))
            time.sleep(5)

    def _give_up(self, run_id: str, why: str) -> None:
        M.AGENT_RUNS.labels(self.agent, "failed").inc()
        finish_run(self.store, self.http, run_id, incomplete_result(run_id, why), status="failed")

    # --------------------------------------------------------- handle
    def handle(self, job: dict) -> None:
        run_id = job["run_id"]
        token = correlation_id.set(run_id)
        t0 = time.perf_counter()
        try:
            with attached(job.get("carrier")), tracer().start_as_current_span(f"{self.agent}.run") as span:
                span.set_attribute("run.id", run_id)
                span.set_attribute("job.attempt", job.get("attempt", 1))
                run = self.store.get(run_id)
                if run is None:
                    log(logger, logging.WARNING, "run not found; dropping job", run_id=run_id)
                    return
                if run.get("status") in ("completed", "failed"):
                    return  # duplicate delivery after a requeue
                ctx = RunContext.create(run_id, self.agent, self.store, self.http)
                self.store.event(run_id, "stage_started", self.agent, worker_id=self.worker_id, attempt=job.get("attempt", 1),
                                 queue_wait_s=round(time.time() - job.get("enqueued_at", time.time()), 3))
                if self.kind == "triage":
                    self._triage(ctx, run, span)
                else:
                    self._liability(ctx, run, span)
                M.AGENT_STEPS.labels(self.agent).observe(ctx.nodes)
                M.AGENT_TOOLS.labels(self.agent).observe(ctx.budget.tool_calls)
                M.AGENT_LLM_CALLS.labels(self.agent).observe(ctx.budget.llm_calls)
                M.AGENT_RUNS.labels(self.agent, "degraded" if ctx.degraded else "success").inc()
        except Exception as exc:
            log(logger, logging.ERROR, "agent run failed", run_id=run_id, error=f"{type(exc).__name__}: {exc}")
            self.store.event(run_id, "error", self.agent, error=f"{type(exc).__name__}: {exc}"[:500], attempt=job.get("attempt", 1))
            if job.get("attempt", 1) < self.s.job_max_attempts:
                M.AGENT_RETRIES.labels(self.agent, "job").inc()
                self.store.enqueue(self.queue, run_id, job.get("carrier"), job.get("attempt", 1) + 1)
            else:
                M.AGENT_RUNS.labels(self.agent, "failed").inc()
                finish_run(self.store, self.http, run_id, incomplete_result(run_id, f"{self.agent} failed: {type(exc).__name__}"), "failed")
        finally:
            M.AGENT_LAT.labels(self.agent).observe(time.perf_counter() - t0)
            correlation_id.reset(token)

    def _triage(self, ctx: RunContext, run: dict, span) -> None:
        from triage.agents.triage_agent import run_triage

        self.store.update(ctx.run_id, status="running", stage="triage")
        brief = run_triage(ctx, run["request"])
        ctx.degraded = brief.degraded
        self.store.update(ctx.run_id, stage="handoff", brief=brief.model_dump(mode="json"),
                          triage_cost_usd=ctx.cost_usd, triage_tokens=ctx.tokens)
        self.store.enqueue(Q_LIABILITY, ctx.run_id, inject_carrier())

    def _liability(self, ctx: RunContext, run: dict, span) -> None:
        from triage.agents.liability_agent import run_liability

        self.store.update(ctx.run_id, status="running", stage="liability")
        brief = TriageBrief(**run["brief"])
        exp = (run["request"].get("experiment") or {}) if self.s.allow_experiment_overrides else {}
        profile = exp.get("retrieval_profile") or self.s.default_retrieval_profile
        variant = exp.get("prompt_variant") or self.s.prompt_variant
        rec, final_state = run_liability(ctx, run["request"], brief, profile, variant)
        # attribute triage cost to the run total
        tri_cost = float(run.get("triage_cost_usd") or 0)
        tri_tok = json.loads(run["triage_tokens"]) if isinstance(run.get("triage_tokens"), str) else (run.get("triage_tokens") or {})
        rec.cost_usd = round(rec.cost_usd + tri_cost, 8)
        rec.tokens = {k: rec.tokens.get(k, 0) + int(tri_tok.get(k, 0)) for k in ("prompt", "completion")}
        ctx.degraded = rec.degraded
        M.RUN_COST.observe(rec.cost_usd)
        M.RUN_TOKENS.labels("prompt").observe(rec.tokens["prompt"])
        M.RUN_TOKENS.labels("completion").observe(rec.tokens["completion"])
        result = rec.model_dump(mode="json")
        result["retrieval_profile"], result["prompt_variant"] = profile, variant
        self.store.update(ctx.run_id, first_draft=final_state.get("first_draft") or {})
        finish_run(self.store, self.http, ctx.run_id, result)
        self._online_eval(ctx, rec, final_state)

    def _online_eval(self, ctx: RunContext, rec: Recommendation, state: dict) -> None:
        """L6: score a sample of live runs with the offline metrics."""
        if random.random() >= self.s.online_eval_sample_rate:
            return
        try:
            from triage.agents.liability_agent import case_text
            from triage.evaluation.metrics import score_run

            passages = {cid: r["text"] for cid, r in (state.get("retrieved") or {}).items()}
            brief = TriageBrief(**state["brief"])
            m = score_run(self.http, self.s.retriever_url, case_text(brief, state["request"]),
                          [c.model_dump() for c in rec.claims], passages,
                          [str(int(state["request"]["transaction"]["amount_inr"])), str(brief.reporting_delay.working_days)])
            for k in ("groundedness", "faithfulness", "context_relevance"):
                if m.get(k) is not None:
                    M.QUALITY.labels(k).observe(m[k])
                    M.QUALITY_LAST.labels(k).set(m[k])
            M.QUALITY_LAST.labels("hallucinated").set(m["hallucinated"])
            self.store.add_quality(ctx.run_id, m)
            self.store.event(ctx.run_id, "online_eval", self.agent, **m)
        except Exception as exc:
            log(logger, logging.WARNING, "online eval failed", error=str(exc))


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else "liability"
    if kind not in ("triage", "liability"):
        print("usage: python -m triage.agents.worker [triage|liability]")
        return 2
    s = get_settings()
    service = "triage-agent" if kind == "triage" else "agent-worker"
    setup_logging(service, s.log_level)
    setup_tracing(service, s.otel_exporter_otlp_endpoint)
    instrument_httpx()
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    Worker(kind).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
