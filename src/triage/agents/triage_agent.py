"""Triage agent (process: `triage-agent`).

Responsibility: everything that can be decided from the dispute itself.
  guard_input -> assess (LLM) -> compute_delay (tool) -> attribute_fault -> handoff

Output: a TriageBrief (contract triage-brief v1), handed to the liability agent
through the `q:liability` queue. The triage agent never sees the corpus.

Tools available to this agent: lookup_request_fields, compute_working_days.
If the model is unavailable, `assess` falls back to keyword rules and the
brief is marked degraded, which forces escalation downstream.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from triage.agents import guardrails as G
from triage.agents import metrics as M
from triage.agents import prompts
from triage.agents.context import BudgetExhausted, LLMUnavailable, RunContext
from triage.agents.rules import attribute_fault
from triage.agents.workdays import working_days_between
from triage.config import get_settings
from triage.contracts.results import Category, Facts, GuardrailReport, ReportingDelay, TriageBrief

AGENT = "triage-agent"
TRI = ("YES", "NO", "UNKNOWN")


class TriageState(TypedDict, total=False):
    request: dict[str, Any]
    guard: dict[str, Any]
    complaint_for_model: str
    category: str
    confidence: float
    rationale: str
    facts: dict[str, Any]
    delay: dict[str, Any]
    fault: str
    contest_reasons: list[str]
    degraded: bool
    brief: dict[str, Any]


# --------------------------------------------------------------------------- tools
def lookup_request_fields(request: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    flat = {
        "amount_inr": request["transaction"]["amount_inr"],
        "channel": request["transaction"]["channel"],
        "auth_method": request["transaction"].get("auth_method", "UNKNOWN"),
        "transaction_at": request["transaction"]["transaction_at"],
        "merchant_name": request["transaction"].get("merchant_name"),
        "duplicate_of_transaction_id": request["transaction"].get("duplicate_of_transaction_id"),
        "account_type": request["customer"]["account_type"],
        "credit_limit_inr": request["customer"].get("credit_limit_inr"),
        "alert_received_at": request["reporting"].get("alert_received_at"),
        "reported_at": request["reporting"]["reported_at"],
        "reporting_channel": request["reporting"]["reporting_channel"],
        **{f"assertion_{k}": v for k, v in (request.get("customer_assertions") or {}).items()},
    }
    return {f: flat.get(f) for f in fields} if fields else flat


def compute_working_days(from_date: str, to_date: str) -> dict[str, Any]:
    f, t = _dt(from_date), _dt(to_date)
    wd, cal, excluded = working_days_between(f, t, get_settings().extra_holidays)
    return {"working_days": wd, "calendar_days": cal, "excluded_non_working_days": excluded}


def _dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- rule fallback
def rule_assess(request: dict[str, Any], text: str) -> dict[str, Any]:
    t = text.lower()
    ch = request["transaction"]["channel"]
    a = request.get("customer_assertions") or {}

    def has(*pats: str) -> bool:
        return any(re.search(p, t) for p in pats)

    if request["transaction"].get("duplicate_of_transaction_id") or has(r"\btwice\b", r"double (charge|debit)", r"duplicate", r"charged again", r"two times"):
        cat = Category.DUPLICATE_CHARGE
    elif has(r"\bcheque\b", r"\bloan\b", r"\bemi\b", r"insurance", r"mutual fund", r"locker", r"cash deposit", r"minimum balance", r"interest rate"):
        cat = Category.OUT_OF_SCOPE
    elif has(r"not (been )?delivered", r"never (received|arrived)", r"refund", r"defective", r"cancell?ed (the )?order", r"merchant (refused|has not)", r"subscription"):
        cat = Category.MERCHANT_DISPUTE
    elif ch == "UPI" or has(r"collect request", r"customer care", r"anydesk", r"teamviewer", r"screen ?shar", r"\bqr\b", r"\bkyc\b"):
        cat = Category.UPI_SOCIAL_ENGINEERING if ch in ("UPI", "MOBILE_BANKING", "NET_BANKING") or has(r"\bupi\b") else Category.CARD_NOT_PRESENT_FRAUD
    elif ch in ("CARD_ATM", "CARD_POS") or has(r"skim", r"clon"):
        cat = Category.SKIMMING
    else:
        cat = Category.CARD_NOT_PRESENT_FRAUD
    shared = "YES" if has(r"(shared|gave|told|disclosed|read out|entered|approved).{0,40}\b(otp|pin|cvv|password|collect request)",
                          r"\b(otp|pin|cvv)\b.{0,30}\b(shared|gave|told)") else (
        "NO" if has(r"(did not|didn't|never|have not|haven't) (share|give|tell|disclose)") else "UNKNOWN")
    imp = "YES" if has(r"(from|claiming|pretending|posing|said he was|said she was|said they were).{0,40}(bank|police|rbi|customer care|courier|income tax|kyc)") else "UNKNOWN"
    facts = {
        "credentials_shared": a.get("credentials_shared") if a.get("credentials_shared") in ("YES", "NO") else shared,
        "shared_with_impersonator": imp,
        "card_in_possession": a.get("card_in_possession", "UNKNOWN"),
        "alert_noticed": "NO" if has(r"(did not|didn't) (notice|see)") else "UNKNOWN",
        "bank_system_failure_alleged": "YES" if has(r"bank'?s? (system|server|staff|employee|error|glitch)", r"breach at the bank") else "UNKNOWN",
        "customer_denies_involvement": "YES" if has(r"(did not|didn't|never) (make|authori[sz]e|do)") else "UNKNOWN",
        "authorised_by_customer": "YES" if cat in (Category.MERCHANT_DISPUTE, Category.DUPLICATE_CHARGE) else "UNKNOWN",
    }
    return {"category": cat.value, "confidence": 0.4, "rationale": "keyword rules (model unavailable)", "facts": facts}


# -------------------------------------------------------------------- graph
def build_graph(ctx: RunContext):
    def guard_input(state: TriageState) -> TriageState:
        with ctx.step("guard_input"):
            text = state["request"]["complaint_text"]
            inj, inj_sig = G.scan_injection(text)
            tox, tox_sig = G.scan_toxicity(text)
            s = get_settings()
            withheld = inj >= s.injection_threshold
            for sig in inj_sig:
                M.GUARDRAIL.labels("injection", sig).inc()
            for sig in tox_sig:
                M.GUARDRAIL.labels("toxicity", sig if sig == "threat" else "abusive_term").inc()
            guard = GuardrailReport(injection_score=inj, injection_signals=inj_sig, toxicity_score=tox,
                                    toxicity_signals=tox_sig, complaint_withheld_from_model=withheld)
            ctx.event("guardrail", guard="input", injection_score=inj, injection_signals=inj_sig,
                      toxicity_score=tox, toxicity_signals=tox_sig, withheld=withheld)
            for_model = ("[Complaint withheld from the model: suspected prompt injection. Use structured fields only.]"
                         if withheld else G.sanitise_for_model(text))
            return {"guard": guard.model_dump(), "complaint_for_model": for_model}

    def assess(state: TriageState) -> TriageState:
        with ctx.step("assess"):
            req = state["request"]
            fields = ctx.tool("lookup_request_fields", lambda fields: lookup_request_fields(req, fields), {"fields": []},
                              summarise=lambda o: o)
            p = prompts.get("triage.assess")
            out: dict[str, Any] | None = None
            degraded = False
            for attempt in range(2):
                try:
                    out = ctx.llm(p, "assess", fields=json.dumps(fields, default=str),
                                  complaint=state["complaint_for_model"])
                    if out.get("category") in Category.__members__:
                        break
                    out = None
                except LLMUnavailable:
                    degraded = True
                    break
                except BudgetExhausted:
                    degraded = True
                    break
                except ValueError:
                    out = None
                M.AGENT_RETRIES.labels(AGENT, "invalid_output").inc()
            if out is None:
                degraded = True
                M.DEGRADED.labels(AGENT).inc()
                out = rule_assess(req, req["complaint_text"] if not state["guard"]["complaint_withheld_from_model"] else "")
                ctx.event("fallback", step="assess", reason="model unavailable or invalid output", using="keyword rules")
            facts = {k: (str(v).upper() if str(v).upper() in TRI else "UNKNOWN") for k, v in (out.get("facts") or {}).items()
                     if k in Facts.model_fields}
            # validated structured assertions take precedence over extraction from free text
            a = req.get("customer_assertions") or {}
            for k in ("credentials_shared", "card_in_possession"):
                if a.get(k) in ("YES", "NO"):
                    facts[k] = a[k]
            if out.get("category") == Category.UPI_SOCIAL_ENGINEERING.value and req["transaction"]["channel"].startswith("CARD_"):
                # categorical consistency with the validated channel field
                fixed = Category.SKIMMING.value if req["transaction"]["channel"] in ("CARD_ATM", "CARD_POS") else Category.CARD_NOT_PRESENT_FRAUD.value
                ctx.event("override", field="category", was=out["category"], now=fixed, reason="UPI category on a card channel")
                out["category"] = fixed
            denies = (a.get("recognises_merchant") == "NO" or facts.get("customer_denies_involvement") == "YES"
                      or (a.get("credentials_shared") == "NO" and a.get("card_in_possession") == "YES"
                          and facts.get("authorised_by_customer") != "YES"))
            if out.get("category") in (Category.MERCHANT_DISPUTE.value, Category.DUPLICATE_CHARGE.value) and denies \
                    and not req["transaction"].get("duplicate_of_transaction_id"):
                # an authorised-payment category contradicts the customer's validated assertions
                ch = req["transaction"]["channel"]
                fixed = (Category.SKIMMING if ch in ("CARD_ATM", "CARD_POS") else Category.CARD_NOT_PRESENT_FRAUD
                         if ch.startswith("CARD_") else Category.UPI_SOCIAL_ENGINEERING).value
                ctx.event("override", field="category", was=out["category"], now=fixed,
                          reason="authorised-payment category contradicts customer assertions")
                out["category"] = fixed
            if req["transaction"].get("duplicate_of_transaction_id") and out.get("category") != Category.DUPLICATE_CHARGE.value:
                ctx.event("override", field="category", reason="duplicate_of_transaction_id present")
                out["category"] = Category.DUPLICATE_CHARGE.value
            return {"category": out["category"], "confidence": float(out.get("confidence") or 0.0),
                    "rationale": str(out.get("rationale", ""))[:400], "facts": facts, "degraded": degraded}

    def compute_delay(state: TriageState) -> TriageState:
        with ctx.step("compute_delay"):
            rep = state["request"]["reporting"]
            start = rep.get("alert_received_at") or state["request"]["transaction"]["transaction_at"]
            basis = "alert_received_at" if rep.get("alert_received_at") else "transaction_at (alert time not supplied)"
            out = ctx.tool("compute_working_days", compute_working_days,
                           {"from_date": str(start), "to_date": str(rep["reported_at"])}, summarise=lambda o: o)
            delay = ReportingDelay(working_days=out["working_days"], calendar_days=out["calendar_days"],
                                   from_date=str(start)[:10], to_date=str(rep["reported_at"])[:10], basis=basis,
                                   excluded_holidays=out["excluded_non_working_days"])
            return {"delay": delay.model_dump()}

    def attribute(state: TriageState) -> TriageState:
        with ctx.step("attribute_fault"):
            facts = Facts(**state["facts"])
            tx = state["request"]["transaction"]
            fault, reasons = attribute_fault(Category(state["category"]), facts, tx.get("auth_method", "UNKNOWN"), tx["channel"])
            ctx.event("fault_hypothesis", fault=fault.value, contest_reasons=reasons, facts=facts.model_dump())
            return {"fault": fault.value, "contest_reasons": reasons}

    def handoff(state: TriageState) -> TriageState:
        with ctx.step("handoff"):
            brief = TriageBrief(
                run_id=ctx.run_id, category=Category(state["category"]), category_confidence=state.get("confidence", 0.0),
                category_rationale=state.get("rationale", ""), facts=Facts(**state["facts"]), fault_hypothesis=state["fault"],
                contest_reasons=state.get("contest_reasons", []), reporting_delay=ReportingDelay(**state["delay"]),
                guardrails=GuardrailReport(**state["guard"]), sanitized_complaint=state["complaint_for_model"],
                degraded=bool(state.get("degraded")), prompt_versions=dict(ctx.prompt_versions),
            )
            ctx.event("handoff", to="agent-worker", brief=brief.model_dump(mode="json"))
            return {"brief": brief.model_dump(mode="json")}

    g = StateGraph(TriageState)
    g.add_node("guard_input", guard_input)
    g.add_node("assess", assess)
    g.add_node("compute_delay", compute_delay)
    g.add_node("attribute_fault", attribute)
    g.add_node("handoff", handoff)
    g.set_entry_point("guard_input")
    g.add_edge("guard_input", "assess")
    g.add_edge("assess", "compute_delay")
    g.add_edge("compute_delay", "attribute_fault")
    g.add_edge("attribute_fault", "handoff")
    g.add_edge("handoff", END)
    return g.compile()


def run_triage(ctx: RunContext, request: dict[str, Any]) -> TriageBrief:
    graph = build_graph(ctx)
    final = graph.invoke({"request": request}, config={"recursion_limit": 12})
    return TriageBrief(**final["brief"])
