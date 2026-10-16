"""Public API (service: `api`).

POST /v1/disputes           validate -> (quarantine | create run + enqueue) -> 202 {run_id}
GET  /v1/runs/{id}          poll status / result
GET  /v1/runs/{id}/trace    full reconstruction of a run (D5)
POST /v1/runs/{id}/feedback analyst feedback (L6)
GET  /v1/feedback/summary   aggregated feedback
GET  /v1/quality            sampled online quality scores over time
GET  /v1/quarantine         rejected requests with field-level errors
GET  /v1/contracts          published data contracts (JSON Schema)
GET  /                      minimal web client (A8)

Why asynchronous submit + poll (A4): a run takes tens of seconds and several
model calls; holding an HTTP request open for that long ties up connections,
breaks behind proxies with 30-60 s idle timeouts and loses the work if the
client disconnects. The API returns a run id at once, the work sits in a Redis
queue between submission and execution, and the caller polls (simple, works
through firewalls) or supplies `callback_url` to be notified.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import redis
from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from triage.agents import prompts as prompt_registry
from triage.api.ratelimit import RateLimiter
from triage.common.logging import correlation_id, log
from triage.common.runstore import Q_LIABILITY, Q_TRIAGE, RunStore
from triage.common.service import create_app
from triage.common.telemetry import inject_carrier, tracer
from triage.config import get_settings
from triage.contracts.dispute import DisputeReport, FeedbackRequest
from triage.contracts.results import Recommendation, TriageBrief
from prometheus_client import Counter, Gauge

logger = logging.getLogger("api")
s = get_settings()
app = create_app("api", "Card dispute triage API", description="Unauthorised transaction and card dispute triage service")
store = RunStore(s.redis_url, s.run_ttl_s)
limiter = RateLimiter(store.r, s.rate_limit_per_minute, s.rate_limit_burst)
STATIC = Path(__file__).parent / "static"

SUBMITTED = Counter("disputes_submitted_total", "Accepted dispute submissions")
REJECTED = Counter("disputes_rejected_total", "Rejected (quarantined) submissions", ["reason"])
RATE_LIMITED = Counter("rate_limited_total", "Requests refused by the per-client rate limiter")
FEEDBACK = Counter("feedback_total", "Analyst feedback", ["satisfactory"])
QDEPTH = Gauge("api_queue_depth", "Queue depth seen by the API", ["queue"])


def client_id(request: Request) -> str:
    cid = request.headers.get("X-Client-ID", "").strip()
    if cid and len(cid) <= 64 and cid.replace("-", "").replace("_", "").isalnum():
        return cid
    return f"ip-{request.client.host if request.client else 'unknown'}"


def field_errors(exc: ValidationError) -> list[dict[str, Any]]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e["loc"])
        msg = e["msg"]
        if e["type"] == "value_error" and "; " in msg or ": " in msg and not loc:
            # cross-field validator: "Value error, a.b: msg; c.d: msg"
            body = msg.split("Value error, ", 1)[-1]
            for part in body.split("; "):
                f, _, m = part.partition(": ")
                out.append({"field": f.strip() or loc or "(body)", "error": m.strip() or part, "type": "cross_field"})
            continue
        out.append({"field": loc or "(body)", "error": msg, "type": e["type"]})
    return out


@app.get("/readyz", tags=["ops"])
def readyz():
    try:
        store.r.ping()
    except redis.RedisError:
        raise HTTPException(503, "redis unavailable")
    return {"status": "ready"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/v1/disputes", status_code=202, tags=["disputes"])
async def submit(request: Request):
    cid = client_id(request)
    ok, retry_after = limiter.allow(cid)
    if not ok:
        RATE_LIMITED.inc()
        return JSONResponse({"error": "rate_limited", "retry_after_s": retry_after}, status_code=429,
                            headers={"Retry-After": str(int(retry_after) + 1)})
    raw = await request.body()
    try:
        body = json.loads(raw or b"null")
    except json.JSONDecodeError as exc:
        qid = store.quarantine(raw.decode("utf-8", "replace")[:5000], [{"field": "(body)", "error": f"invalid JSON: {exc}", "type": "json"}], cid)
        REJECTED.labels("invalid_json").inc()
        return JSONResponse({"error": "invalid_json", "quarantine_id": qid}, status_code=400)
    try:
        report = DisputeReport.model_validate(body)
    except ValidationError as exc:
        errors = field_errors(exc)
        qid = store.quarantine(body, errors, cid)
        REJECTED.labels("validation").inc()
        log(logger, logging.INFO, "request quarantined", quarantine_id=qid, errors=len(errors))
        return JSONResponse({"error": "validation_failed", "quarantine_id": qid, "errors": errors,
                             "note": "No agent run was started."}, status_code=422)
    depth = store.queue_depth(Q_TRIAGE)
    QDEPTH.labels(Q_TRIAGE).set(depth)
    QDEPTH.labels(Q_LIABILITY).set(store.queue_depth(Q_LIABILITY))
    if depth >= s.max_queue_depth:
        return JSONResponse({"error": "overloaded", "queue_depth": depth}, status_code=503, headers={"Retry-After": "10"})
    payload = report.model_dump(mode="json")
    with tracer().start_as_current_span("api.submit") as span:
        run_id = store.create_run(payload, cid)
        existing = store.idempotent_run(cid, report.client_reference, run_id)
        if existing:
            store.r.delete(f"run:{run_id}")
            return JSONResponse({"run_id": existing, "status": "duplicate", "status_url": f"/v1/runs/{existing}"}, status_code=200)
        correlation_id.set(run_id)
        span.set_attribute("run.id", run_id)
        store.event(run_id, "run_submitted", "api", client_id=cid, client_reference=report.client_reference,
                    schema_version=report.schema_version)
        store.enqueue(Q_TRIAGE, run_id, inject_carrier())
    SUBMITTED.inc()
    return {"run_id": run_id, "status": "queued", "status_url": f"/v1/runs/{run_id}", "trace_url": f"/v1/runs/{run_id}/trace"}


@app.get("/v1/runs/{run_id}", tags=["disputes"])
def get_run(run_id: str):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    out = {k: run.get(k) for k in ("run_id", "status", "stage", "created_at", "updated_at", "completed_at")}
    if run.get("completed_at"):
        out["elapsed_s"] = round(run["completed_at"] - run["created_at"], 3)
    else:
        out["elapsed_s"] = round(time.time() - run["created_at"], 3)
    if run.get("result"):
        out["result"] = run["result"]
    return out


@app.get("/v1/runs/{run_id}/trace", tags=["disputes"])
def trace(run_id: str):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    events = store.events(run_id)
    t0 = run["created_at"]
    tools = [e for e in events if e["kind"] == "tool_call"]
    llm = [e for e in events if e["kind"] == "llm_call"]
    passages: dict[str, Any] = {}
    for e in tools:
        for p in ((e.get("result") or {}).get("passages") or []) if isinstance(e.get("result"), dict) else []:
            passages.setdefault(p["chunk_id"], {"label": p["label"], "text": p["text"], "first_seen_via": e["tool"]})
    summary = {
        "run_id": run_id, "status": run.get("status"),
        "prompt_versions": sorted({e["prompt"] for e in llm if e.get("prompt")}),
        "tool_calls": [{"agent": e["agent"], "tool": e["tool"], "args": e.get("args"), "ok": e.get("ok"),
                        "latency_ms": e.get("latency_ms"),
                        "returned": [p["chunk_id"] for p in ((e.get("result") or {}).get("passages") or [])] if isinstance(e.get("result"), dict) else e.get("result")}
                       for e in tools],
        "queries": [e["args"]["query"] for e in tools if e["tool"] == "search_corpus" and e.get("args", {}).get("query")],
        "llm_calls": [{"agent": e["agent"], "step": e.get("step"), "prompt": e.get("prompt"), "provider": e.get("provider"),
                       "cached": e.get("cached"), "prompt_tokens": e.get("prompt_tokens"), "completion_tokens": e.get("completion_tokens"),
                       "cost_usd": e.get("cost_usd"), "latency_ms": e.get("latency_ms"), "ok": e.get("ok"), "retries": e.get("retries")}
                      for e in llm],
        "steps": [{"agent": e["agent"], "step": e["step"], "latency_ms": e["latency_ms"], "at_s": round(e["ts"] - t0, 3),
                   "error": e.get("error")} for e in events if e["kind"] == "step"],
        "verification": [e for e in events if e["kind"] == "verification"],
        "cost_usd_total": round(sum(float(e.get("cost_usd") or 0) for e in llm), 8),
        "cost_by_step": {},
        "passages": passages,
    }
    for e in llm:
        k = f"{e['agent']}:{e.get('step')}"
        summary["cost_by_step"][k] = round(summary["cost_by_step"].get(k, 0) + float(e.get("cost_usd") or 0), 8)
    return {"summary": summary, "events": events, "request": run.get("request"), "brief": run.get("brief"),
            "result": run.get("result")}


@app.post("/v1/runs/{run_id}/feedback", tags=["feedback"])
def feedback(run_id: str, fb: FeedbackRequest):
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.get("status") not in ("completed", "failed"):
        raise HTTPException(409, "run not finished")
    result = run.get("result") or {}
    store.add_feedback(run_id, {**fb.model_dump(), "route": result.get("route"),
                                "prompt_versions": result.get("prompt_versions")})
    FEEDBACK.labels(str(fb.satisfactory).lower()).inc()
    return {"recorded": True}


@app.get("/v1/feedback/summary", tags=["feedback"])
def feedback_summary():
    items = store.feedback()
    by_route: dict[str, dict[str, int]] = {}
    by_prompt: dict[str, dict[str, int]] = {}
    for it in items:
        k = "satisfactory" if it["satisfactory"] else "unsatisfactory"
        by_route.setdefault(it.get("route") or "?", {"satisfactory": 0, "unsatisfactory": 0})[k] += 1
        pv = (it.get("prompt_versions") or {}).get("liability.draft", "?")
        by_prompt.setdefault(pv, {"satisfactory": 0, "unsatisfactory": 0})[k] += 1
    n = len(items)
    sat = sum(1 for i in items if i["satisfactory"])
    return {"total": n, "satisfactory_rate": round(sat / n, 4) if n else None, "by_route": by_route,
            "by_prompt_version": by_prompt, "recent_reasons": [{"run_id": i["run_id"], "satisfactory": i["satisfactory"],
                                                                "reason": i["reason"]} for i in items[-20:]]}


@app.get("/v1/quality", tags=["feedback"])
def quality(limit: int = Query(200, le=5000)):
    return {"series": store.quality_series(limit)}


@app.get("/v1/quarantine", tags=["ops"])
def quarantine(limit: int = Query(20, le=500)):
    return {"items": store.quarantined(limit)}


@app.get("/v1/contracts", tags=["contracts"])
def contracts():
    return {
        "dispute-report.v1": DisputeReport.model_json_schema(),
        "feedback.v1": FeedbackRequest.model_json_schema(),
        "triage-brief.v1": TriageBrief.model_json_schema(),
        "run-result.v1": Recommendation.model_json_schema(),
        "prompts": prompt_registry.catalogue(),
    }
