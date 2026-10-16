"""Per-run execution context shared by the agent graphs: loop bounds (A1),
LLM calls through the gateway (A5, L5), tool calls with timeouts, and the event
log that makes a run reconstructible (D5)."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from triage.agents import metrics as M
from triage.agents.prompts import Prompt
from triage.common.http import UpstreamError, post_json
from triage.common.logging import log
from triage.common.runstore import RunStore
from triage.common.telemetry import tracer
from triage.config import get_settings

logger = logging.getLogger("agent")


class LLMUnavailable(RuntimeError):
    """Gateway refused (circuit open / provider down) or the call failed after retries."""


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class Budget:
    max_steps: int
    max_tool_calls: int
    deadline: float
    steps: int = 0
    tool_calls: int = 0
    llm_calls: int = 0

    def remaining_s(self) -> float:
        return self.deadline - time.time()

    def can_step(self) -> bool:
        return self.steps < self.max_steps and self.remaining_s() > 0

    def can_call_tool(self) -> bool:
        return self.tool_calls < self.max_tool_calls and self.remaining_s() > 0

    def describe(self) -> str:
        return (f"steps {self.steps}/{self.max_steps}, tool calls {self.tool_calls}/{self.max_tool_calls}, "
                f"{max(0, int(self.remaining_s()))}s left")


@dataclass
class RunContext:
    run_id: str
    agent: str
    store: RunStore
    http: httpx.Client
    budget: Budget
    prompt_versions: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0})
    cost_usd: float = 0.0
    degraded: bool = False
    nodes: int = 0          # graph nodes executed (steps per run metric)

    @classmethod
    def create(cls, run_id: str, agent: str, store: RunStore, http: httpx.Client) -> "RunContext":
        s = get_settings()
        return cls(run_id, agent, store, http,
                   Budget(s.agent_max_steps, s.agent_max_tool_calls, time.time() + s.agent_wall_clock_s))

    # ----------------------------------------------------------- events
    def event(self, kind: str, **data: Any) -> None:
        self.store.event(self.run_id, kind, self.agent, **data)

    def step(self, name: str) -> "_Step":
        return _Step(self, name)

    # -------------------------------------------------------------- LLM
    def llm(self, prompt: Prompt, step: str, timeout_s: float | None = None, **vars: str) -> dict[str, Any]:
        """Call the gateway and parse a JSON object. Raises LLMUnavailable."""
        s = get_settings()
        if self.budget.remaining_s() <= 1:
            raise BudgetExhausted("wall-clock budget exhausted before LLM call")
        messages = prompt.render(**vars)
        payload = {
            "messages": messages, "prompt_id": prompt.id, "prompt_version": f"{prompt.version}#{prompt.sha}",
            "run_id": self.run_id, "agent": self.agent, "step": step, "max_tokens": prompt.max_tokens,
            "temperature": 0.0, "json_mode": True,
            "timeout_s": min(s.llm_timeout_s, max(2.0, self.budget.remaining_s())),
        }
        self.prompt_versions[prompt.id] = prompt.ref
        t0 = time.perf_counter()
        self.budget.llm_calls += 1
        M.AGENT_CALLS.labels(self.agent, "llm").inc()

        def on_retry(attempt: int, exc: Exception) -> None:
            M.AGENT_RETRIES.labels(self.agent, "llm_gateway").inc()
            self.event("retry", target="llm-gateway", step=step, attempt=attempt, error=str(exc)[:200])

        with tracer().start_as_current_span(f"llm:{step}") as span:
            span.set_attribute("prompt.ref", prompt.ref)
            try:
                resp = post_json(self.http, f"{s.llm_gateway_url}/v1/chat", payload,
                                 timeout_s=timeout_s or s.gateway_call_timeout_s, retries=s.gateway_call_retries,
                                 on_retry=on_retry)
            except UpstreamError as exc:
                latency = time.perf_counter() - t0
                self.event("llm_call", step=step, prompt=prompt.ref, ok=False, error=str(exc)[:300],
                           detail=str(exc.body)[:300] if exc.body else None, latency_ms=round(latency * 1000, 1))
                raise LLMUnavailable(str(exc)) from exc
            latency = time.perf_counter() - t0
            usage = resp.get("usage", {})
            pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
            self.tokens["prompt"] += pt
            self.tokens["completion"] += ct
            self.cost_usd += float(resp.get("cost_usd", 0.0))
            M.STEP_COST.labels(self.agent, step).inc(float(resp.get("cost_usd", 0.0)))
            if resp.get("retries"):
                M.AGENT_RETRIES.labels(self.agent, "provider").inc(resp["retries"])
            span.set_attribute("llm.prompt_tokens", pt)
            span.set_attribute("llm.completion_tokens", ct)
            content = resp.get("content", "")
            parsed = parse_json_object(content)
            self.event("llm_call", step=step, prompt=prompt.ref, ok=parsed is not None, provider=resp.get("provider"),
                       cached=resp.get("cached"), retries=resp.get("retries", 0), prompt_tokens=pt,
                       completion_tokens=ct, cost_usd=resp.get("cost_usd", 0.0), latency_ms=round(latency * 1000, 1),
                       gateway_latency_ms=resp.get("latency_ms"), output=content[:3000])
            if parsed is None:
                raise ValueError(f"model returned non-JSON output for {prompt.id}")
            return parsed

    # ------------------------------------------------------------- tools
    def tool(self, name: str, fn: Callable[..., Any], args: dict[str, Any], summarise: Callable[[Any], Any]) -> Any:
        if not self.budget.can_call_tool():
            raise BudgetExhausted(f"tool budget exhausted ({self.budget.describe()})")
        self.budget.tool_calls += 1
        M.AGENT_CALLS.labels(self.agent, "tool").inc()
        t0 = time.perf_counter()
        with tracer().start_as_current_span(f"tool:{name}") as span:
            span.set_attribute("tool.args", json.dumps(args)[:300])
            try:
                out = fn(**args)
                ok, err = True, None
            except Exception as exc:  # tool failures are observations, not crashes
                out, ok, err = None, False, f"{type(exc).__name__}: {exc}"
            latency = time.perf_counter() - t0
            M.TOOL_CALLS.labels(self.agent, name, "ok" if ok else "error").inc()
            M.AGENT_STEP_LAT.labels(self.agent, f"tool:{name}").observe(latency)
            self.event("tool_call", tool=name, args=args, ok=ok, error=err, latency_ms=round(latency * 1000, 1),
                       result=summarise(out) if ok else None)
            if not ok:
                log(logger, logging.WARNING, "tool call failed", tool=name, error=err)
                raise ToolError(err or "tool failed")
            return out


class ToolError(RuntimeError):
    pass


class _Step:
    def __init__(self, ctx: RunContext, name: str):
        self.ctx, self.name = ctx, name

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.ctx.nodes += 1
        self.span = tracer().start_as_current_span(f"step:{self.ctx.agent}.{self.name}")
        self.span.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        latency = time.perf_counter() - self.t0
        M.AGENT_STEP_LAT.labels(self.ctx.agent, self.name).observe(latency)
        self.ctx.event("step", step=self.name, latency_ms=round(latency * 1000, 1),
                       error=f"{exc_type.__name__}: {exc}" if exc_type else None)
        self.span.__exit__(exc_type, exc, tb)
        return False


def parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start : i + 1])
                        if isinstance(v, dict):
                            return v
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    return None
