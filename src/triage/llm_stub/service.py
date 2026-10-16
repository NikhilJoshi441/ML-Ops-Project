"""LLM stub (service: `llm-stub`): an OpenAI-compatible endpoint that returns
fixed responses after a realistic, log-normally distributed delay (D6).

Used for load testing so the measurement is of this system, not of a
provider's rate limits, and as a zero-cost provider for local development.
Responses are keyed on the PROMPT-ID line every prompt carries; the planner
follows a fixed script (search -> related provisions -> finish).
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import re

from fastapi import Request
from prometheus_client import Counter

from triage.common.service import create_app
from triage.config import get_settings

s = get_settings()
app = create_app("llm-stub", "LLM stub")
CALLS = Counter("stub_calls_total", "Stub completions", ["prompt_id"])
_mode = {"value": "ok"}

ASSESS = {"category": "CARD_NOT_PRESENT_FRAUD", "confidence": 0.8, "rationale": "stub",
          "facts": {"credentials_shared": "NO", "shared_with_impersonator": "NO", "card_in_possession": "YES",
                    "alert_noticed": "YES", "bank_system_failure_alleged": "NO", "customer_denies_involvement": "YES",
                    "authorised_by_customer": "NO"}}


def _draft(user: str) -> dict:
    ids = re.findall(r"^(\S+:\S+) \|", user, re.M)
    pick = next((i for i in ids if "6(ii)" in i), ids[0] if ids else "RBI-CIRC-2017:6(ii)")
    return {"fault_attribution": "THIRD_PARTY", "liability_band": "ZERO", "route": "PROVISIONAL_CREDIT",
            "customer_liability_inr": 0, "summary": "Stub draft.", "basis": "RBI Circular 2017 para 6(ii).",
            "claims": [{"text": "A customer is entitled to zero liability for a third party breach where the deficiency lies "
                                "neither with the bank nor with the customer and the customer notifies the bank within three "
                                "working days of receiving the communication from the bank.", "citations": [pick]}],
            "escalation_reasons": []}


def _plan(user: str) -> dict:
    n = user.count(") -> ")
    if n == 0:
        return {"thought": "find zero-liability provision", "action": "search_corpus",
                "args": {"query": "third party breach notified within three working days zero liability"}}
    if n == 1:
        return {"thought": "check modifiers", "action": "get_related_provisions", "args": {"clause_id": "RBI-CIRC-2017:6(ii)"}}
    return {"thought": "done", "action": "finish", "args": {}}


@app.post("/admin/mode")
async def mode(body: dict):
    _mode["value"] = body.get("mode", "ok")
    return _mode


@app.post("/v1/chat/completions")
async def completions(req: Request):
    body = await req.json()
    msgs = body.get("messages", [])
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user = next((m["content"] for m in msgs if m["role"] == "user"), "")
    m = re.search(r"PROMPT-ID: ([\w.]+)@", system)
    pid = m.group(1) if m else "unknown"
    CALLS.labels(pid).inc()
    mu = math.log(max(1.0, s.stub_delay_mean_ms)) - s.stub_delay_sigma**2 / 2
    await asyncio.sleep(random.lognormvariate(mu, s.stub_delay_sigma) / 1000)
    if _mode["value"] == "error":
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "stub forced error"}, status_code=503)
    content = {"triage.assess": ASSESS, "liability.plan": _plan(user), "liability.draft": _draft(user)}.get(pid, {"ok": True})
    text = json.dumps(content)
    pt = sum(len(x.get("content", "")) for x in msgs) // 4
    return {"id": "stub", "object": "chat.completion", "model": body.get("model", "stub"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": len(text) // 4, "total_tokens": pt + len(text) // 4}}
