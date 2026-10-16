"""LLM gateway: the one interface to model providers that we control (brief, section 12).

* provider chosen by configuration only (any OpenAI-compatible endpoint: a
  hosted free tier, a local Ollama, or the llm-stub used for load tests)
* per-call timeout, bounded retry with exponential backoff and jitter (A5)
* Redis-backed circuit breaker per provider; when the primary is open the
  optional fallback provider is tried; when both are unavailable the gateway
  answers 503 `circuit_open` immediately and the agents take the deterministic
  degraded path (A5)
* response cache for deterministic (temperature 0) calls (D7)
* token and cost accounting at call time, attributed to run / agent / step (L5)
* chaos switch to demonstrate the breaker on request

Deployment boundary rationale: it is the only service that holds provider
credentials, and the breaker and cache must be shared by all agent replicas.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from typing import Any, Optional

import httpx
import redis
from fastapi import HTTPException
from prometheus_client import Counter, Gauge, Histogram
from pydantic import BaseModel, Field

from triage.common.logging import log
from triage.common.service import create_app
from triage.common.telemetry import tracer
from triage.config import get_settings
from triage.llm_gateway.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker

logger = logging.getLogger("llm-gateway")
s = get_settings()
app = create_app("llm-gateway", "LLM gateway")
r = redis.Redis.from_url(s.redis_url, socket_timeout=1, socket_connect_timeout=1, decode_responses=True)
http = httpx.Client(limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))

LLM_REQ = Counter("llm_requests_total", "LLM provider calls", ["provider", "outcome"])
LLM_LAT = Histogram("llm_call_latency_seconds", "Provider call latency (per attempt)", ["provider"],
                    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60))
LLM_RETRY = Counter("llm_retries_total", "Retries against providers", ["provider"])
LLM_TOK = Counter("llm_tokens_total", "Tokens by agent/step", ["agent", "step", "kind"])
LLM_COST = Counter("llm_cost_usd_total", "Cost in USD by agent/step", ["agent", "step"])
LLM_CACHE = Counter("llm_cache_total", "Gateway response cache", ["result"])
LLM_CACHE_SAVED = Counter("llm_cache_saved_usd_total", "Cost avoided by cache hits")
CB_STATE = Gauge("circuit_breaker_state", "0=closed 1=half_open 2=open", ["provider"])
STATE_NUM = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


class Provider:
    def __init__(self, name: str, base_url: str, api_key: str, model: str):
        self.name, self.base_url, self.api_key, self.model = name, base_url.rstrip("/"), api_key, model
        self.breaker = CircuitBreaker(r, name, s.breaker_failure_threshold, s.breaker_window_s, s.breaker_open_s)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def call(self, req: "ChatReq") -> dict[str, Any]:
        chaos = r.get(f"chaos:{self.name}") or "ok"
        if chaos == "down":
            raise httpx.ConnectError("chaos: provider marked down")
        if chaos == "slow":
            time.sleep(min(req.timeout_s or s.llm_timeout_s, s.llm_timeout_s) + 0.1)
            raise httpx.ReadTimeout("chaos: provider slow")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": req.messages,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = http.post(f"{self.base_url}/chat/completions", json=body, headers=headers,
                         timeout=req.timeout_s or s.llm_timeout_s)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise httpx.HTTPStatusError(f"provider status {resp.status_code}", request=resp.request, response=resp)
        if resp.status_code >= 400:
            raise ValueError(f"provider rejected request: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or sum(len(m.get("content", "")) for m in req.messages) // 4)
        ct = int(usage.get("completion_tokens") or len(content) // 4)
        return {"content": content, "prompt_tokens": pt, "completion_tokens": ct}


primary = Provider("primary", s.llm_base_url, s.llm_api_key, s.llm_model)
fallback = Provider("fallback", s.llm_fallback_base_url, s.llm_fallback_api_key, s.llm_fallback_model)


class ChatReq(BaseModel):
    messages: list[dict[str, str]] = Field(min_length=1, max_length=40)
    prompt_id: str
    prompt_version: str
    run_id: str = "-"
    agent: str = "-"
    step: str = "-"
    max_tokens: int = Field(default=700, ge=16, le=4000)
    temperature: float = Field(default=0.0, ge=0, le=1.5)
    json_mode: bool = True
    timeout_s: Optional[float] = Field(default=None, gt=0, le=300)
    use_cache: bool = True


def _cost(pt: int, ct: int) -> float:
    return pt / 1e6 * s.llm_price_prompt_per_1m + ct / 1e6 * s.llm_price_completion_per_1m


def _cache_key(p: Provider, req: ChatReq) -> str:
    raw = json.dumps([p.model, req.messages, req.temperature, req.max_tokens, req.json_mode], sort_keys=True)
    return "llmcache:" + hashlib.sha256(raw.encode()).hexdigest()


def _try_provider(p: Provider, req: ChatReq) -> tuple[Optional[dict[str, Any]], int, str]:
    """Returns (result, retries, failure_reason)."""
    retries = 0
    last = ""
    for attempt in range(1 + s.llm_max_retries):
        if not p.breaker.allow():
            CB_STATE.labels(p.name).set(STATE_NUM[p.breaker.state()])
            return None, retries, "circuit_open"
        t0 = time.perf_counter()
        try:
            with tracer().start_as_current_span(f"llm.{p.name}.attempt") as span:
                span.set_attribute("llm.attempt", attempt + 1)
                out = p.call(req)
            LLM_LAT.labels(p.name).observe(time.perf_counter() - t0)
            LLM_REQ.labels(p.name, "ok").inc()
            p.breaker.record_success()
            CB_STATE.labels(p.name).set(STATE_NUM[p.breaker.state()])
            return out, retries, ""
        except ValueError as exc:  # non-retryable request error (bad request) - not a provider health signal
            LLM_REQ.labels(p.name, "rejected").inc()
            return None, retries, f"rejected: {exc}"
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            LLM_LAT.labels(p.name).observe(time.perf_counter() - t0)
            outcome = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
            LLM_REQ.labels(p.name, outcome).inc()
            p.breaker.record_failure()
            CB_STATE.labels(p.name).set(STATE_NUM[p.breaker.state()])
            last = f"{outcome}: {exc}"
            log(logger, logging.WARNING, "provider call failed", provider=p.name, attempt=attempt + 1, error=str(exc))
            if attempt < s.llm_max_retries:
                retries += 1
                LLM_RETRY.labels(p.name).inc()
                delay = min(s.llm_backoff_max_s, s.llm_backoff_base_s * (2**attempt))
                time.sleep(delay * (0.5 + random.random() / 2))
    return None, retries, last


@app.get("/readyz", tags=["ops"])
def readyz():
    try:
        r.ping()
    except redis.RedisError:
        raise HTTPException(503, "redis unavailable")
    if not primary.configured:
        raise HTTPException(503, "LLM_BASE_URL / LLM_MODEL not configured")
    return {"status": "ready", "primary": primary.breaker.state()}


@app.post("/v1/chat")
def chat(req: ChatReq):
    t0 = time.perf_counter()
    providers = [p for p in (primary, fallback) if p.configured]
    if not providers:
        raise HTTPException(503, {"error": "not_configured"})
    total_retries = 0
    reasons: dict[str, str] = {}
    with tracer().start_as_current_span("llm.chat") as span:
        span.set_attribute("llm.prompt_id", req.prompt_id)
        span.set_attribute("llm.prompt_version", req.prompt_version)
        span.set_attribute("run.id", req.run_id)
        for p in providers:
            key = _cache_key(p, req)
            cacheable = s.llm_cache_enabled and req.use_cache and req.temperature == 0
            if cacheable:
                try:
                    hit = r.get(key)
                except redis.RedisError:
                    hit = None
                if hit:
                    data = json.loads(hit)
                    LLM_CACHE.labels("hit").inc()
                    LLM_CACHE_SAVED.inc(_cost(data["prompt_tokens"], data["completion_tokens"]))
                    span.set_attribute("llm.cached", True)
                    return {
                        "content": data["content"], "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                                              "cached_prompt_tokens": data["prompt_tokens"],
                                                              "cached_completion_tokens": data["completion_tokens"]},
                        "cost_usd": 0.0, "provider": p.name, "cached": True, "retries": 0,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    }
                LLM_CACHE.labels("miss").inc()
            out, retries, reason = _try_provider(p, req)
            total_retries += retries
            if out is None:
                reasons[p.name] = reason
                continue
            cost = _cost(out["prompt_tokens"], out["completion_tokens"])
            LLM_TOK.labels(req.agent, req.step, "prompt").inc(out["prompt_tokens"])
            LLM_TOK.labels(req.agent, req.step, "completion").inc(out["completion_tokens"])
            LLM_COST.labels(req.agent, req.step).inc(cost)
            if cacheable:
                try:
                    r.setex(key, s.llm_cache_ttl_s, json.dumps(out))
                except redis.RedisError:
                    pass
            span.set_attribute("llm.provider", p.name)
            span.set_attribute("llm.prompt_tokens", out["prompt_tokens"])
            span.set_attribute("llm.completion_tokens", out["completion_tokens"])
            return {
                "content": out["content"],
                "usage": {"prompt_tokens": out["prompt_tokens"], "completion_tokens": out["completion_tokens"]},
                "cost_usd": round(cost, 8), "provider": p.name, "cached": False, "retries": total_retries,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
    all_open = all(v == "circuit_open" for v in reasons.values())
    raise HTTPException(503, {"error": "circuit_open" if all_open else "provider_unavailable",
                              "reasons": reasons, "retries": total_retries})


@app.get("/admin/breaker", tags=["ops"])
def breaker_state():
    return {p.name: p.breaker.snapshot() | {"configured": p.configured, "chaos": r.get(f"chaos:{p.name}") or "ok"}
            for p in (primary, fallback)}


class ChaosReq(BaseModel):
    provider: str = Field(default="primary", pattern="^(primary|fallback)$")
    mode: str = Field(pattern="^(ok|down|slow)$")


@app.post("/admin/chaos", tags=["ops"])
def chaos(req: ChaosReq):
    """Demonstration switch: make a provider fail (`down`) or time out (`slow`)."""
    if req.mode == "ok":
        r.delete(f"chaos:{req.provider}")
    else:
        r.set(f"chaos:{req.provider}", req.mode, ex=3600)
    log(logger, logging.WARNING, "chaos mode set", provider=req.provider, mode=req.mode)
    return breaker_state()


@app.post("/admin/breaker/reset", tags=["ops"])
def breaker_reset():
    for p in (primary, fallback):
        p.breaker.reset()
        CB_STATE.labels(p.name).set(0)
    return breaker_state()
