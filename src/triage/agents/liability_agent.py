"""Liability agent (process: `agent-worker`).

Responsibility: find the provisions that govern the case, draft a liability
position grounded in them, verify it, and route.

    plan <-> act        ReAct loop: the model chooses a tool each turn (A1)
      |
    coverage            step 2/3 of the method: are there provisions that modify
      |                 what was found? if gaps remain -> plan again (once), then
      |                 a deterministic expansion strategy
    draft               model drafts the position with chunk-level citations
      |
    verify              every citation resolves to retrieved text, every claim is
      |                 supported by its cited passage, the governing provisions
      |                 are cited, and band/route agree with the liability table (A3)
      +-- fail -> replan with a different strategy (expand, then targeted) -> draft
      +-- fail after max attempts -> escalate (VERIFICATION_FAILED)
    finalize            output guardrails, escalation rules, recommendation

Loop bounds: planner turns, tool calls and wall-clock (config AGENT_MAX_*).
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from triage.agents import guardrails as G
from triage.agents import metrics as M
from triage.agents import prompts
from triage.agents import rules as R
from triage.agents.context import BudgetExhausted, LLMUnavailable, RunContext, ToolError
from triage.common.http import get_json, post_json
from triage.config import get_settings
from triage.contracts.results import (
    Category, Citation, Claim, EscalationReason, Fault, LiabilityBand, Recommendation, Route, TriageBrief,
    VerificationReport,
)

AGENT = "agent-worker"
SUPPORTING = {"WORKING_DAYS", "SHADOW_REVERSAL", "BURDEN_OF_PROOF", "LIABILITY_CAP_TABLE"}
STRATEGIES = ["agentic", "expand", "targeted"]
ESCALATION_PATH = ("Refer to a disputes analyst (L2) to attribute fault and decide liability. The customer must be told "
                   "the outcome; if the bank does not reply within 30 days or the customer is dissatisfied, the customer "
                   "may complain to the RBI Ombudsman under the Integrated Ombudsman Scheme 2021.")


class LState(TypedDict, total=False):
    brief: dict[str, Any]
    request: dict[str, Any]
    profile: str
    variant: str
    rule: dict[str, Any]
    retrieved: dict[str, dict[str, Any]]
    history: list[str]
    strategy_idx: int
    phase_turns: int
    coverage_rounds: int
    action: dict[str, Any]
    max_dense: float
    draft: dict[str, Any]
    verification: dict[str, Any]
    attempts: int
    feedback: str
    first_draft: dict[str, Any]
    degraded: bool
    stop_reason: str
    final: dict[str, Any]


# ===================================================================== tools
class Tools:
    """Read-only tools. Arguments are validated; nothing here can write or decide."""

    def __init__(self, ctx: RunContext, profile: str, request: dict[str, Any], brief: TriageBrief):
        self.ctx, self.profile, self.request, self.brief = ctx, profile, request, brief
        self.s = get_settings()

    def search_corpus(self, query: str, mode: str = "hybrid") -> dict[str, Any]:
        q = str(query)[:400]
        if len(q.strip()) < 3:
            raise ValueError("query too short")
        return post_json(self.ctx.http, f"{self.s.retriever_url}/v1/search",
                         {"query": q, "k": 5, "profile": self.profile, "mode": mode},
                         timeout_s=self.s.tool_timeout_s, retries=1)

    def get_clause(self, clause_id: str) -> dict[str, Any]:
        cid = str(clause_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9\-:().~ ]{2,60}", cid):
            raise ValueError("invalid clause id")
        return get_json(self.ctx.http, f"{self.s.retriever_url}/v1/clause/{cid}?profile={self.profile}",
                        timeout_s=self.s.tool_timeout_s)

    def get_related_provisions(self, clause_id: str) -> dict[str, Any]:
        return post_json(self.ctx.http, f"{self.s.retriever_url}/v1/related",
                         {"clause_id": str(clause_id).strip()[:60], "profile": self.profile},
                         timeout_s=self.s.tool_timeout_s, retries=1)

    def covered(self, key: str, passages: dict[str, dict[str, Any]]) -> list[str]:
        """Chunk ids among `passages` that contain provision family `key`. Clause-profile ids match
        directly; other chunkings (e.g. the naive baseline) match by the clause's text content."""
        hits = R.covered(key, set(passages))
        if hits or self.profile == "clause":
            return hits
        sigs = self._signatures(key)
        return [cid for cid, p in passages.items() if any(sg in _alnum(p.get("text", "")) for sg in sigs)]

    def _signatures(self, key: str) -> list[str]:
        cache = _SIGNATURES.setdefault(key, [])
        if not cache:
            for cl in R.family_clauses(key):
                try:
                    recs = get_json(self.ctx.http, f"{self.s.retriever_url}/v1/clause/{cl}?profile=clause",
                                    timeout_s=self.s.tool_timeout_s)["chunks"]
                except Exception:
                    continue
                t = _alnum(recs[0]["text"])
                cache.extend(t[i:i + 40] for i in (8, len(t) // 2, max(8, len(t) - 60)) if len(t) > i + 40)
        return cache

    def catalogue(self) -> str:
        if not hasattr(self, "_catalogue"):
            try:
                cat = get_json(self.ctx.http, f"{self.s.retriever_url}/v1/catalogue?profile={self.profile}",
                               timeout_s=self.s.tool_timeout_s)["clauses"]
                self._catalogue = "\n".join(f"{c['clause_id']}: {c['hint']}" for c in cat[:70])
            except Exception:
                self._catalogue = "(unavailable)"
        return self._catalogue

    def lookup_request_fields(self, fields: list[str] | None = None) -> dict[str, Any]:
        from triage.agents.triage_agent import lookup_request_fields

        flat = lookup_request_fields(self.request, [])
        flat["reporting_delay_working_days"] = self.brief.reporting_delay.working_days
        flat["reporting_delay_basis"] = self.brief.reporting_delay.basis
        flat["category"] = self.brief.category.value
        flat["fault_hypothesis"] = self.brief.fault_hypothesis.value
        fields = [f for f in (fields or []) if isinstance(f, str)]
        return {f: flat.get(f) for f in fields} if fields else flat


_SIGNATURES: dict[str, list[str]] = {}


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _passages_from(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    if "hits" in result:
        return result["hits"]
    if "chunks" in result:
        return result["chunks"]
    if "related" in result:
        return result["related"]
    return []


def _summ(result: Any) -> Any:
    ps = _passages_from(result)
    if ps:
        return {"passages": [{"chunk_id": p["chunk_id"], "label": p["label"], "relation": p.get("relation"),
                              "dense_score": p.get("dense_score"), "text": p["text"]} for p in ps],
                "max_dense_score": result.get("max_dense_score") if isinstance(result, dict) else None,
                "cached": result.get("cached") if isinstance(result, dict) else None}
    return result


# ================================================================== helpers
def case_text(brief: TriageBrief, request: dict[str, Any]) -> str:
    tx, cu = request["transaction"], request["customer"]
    d = brief.reporting_delay
    lines = [
        f"Category: {brief.category.value} (confidence {brief.category_confidence:.2f})",
        f"Channel: {tx['channel']}; authentication: {tx.get('auth_method', 'UNKNOWN')}; amount: INR {tx['amount_inr']:,.0f}",
        f"Account type: {cu['account_type']}" + (f"; credit limit INR {cu['credit_limit_inr']:,.0f}" if cu.get("credit_limit_inr") else ""),
        f"Reporting delay: {d.working_days} working days ({d.calendar_days} calendar days) from {d.from_date} to {d.to_date}; basis: {d.basis}",
        "Facts: " + ", ".join(f"{k}={v}" for k, v in brief.facts.model_dump().items() if k != "signals"),
        f"Fault hypothesis from triage: {brief.fault_hypothesis.value}",
    ]
    if brief.contest_reasons:
        lines.append("Why fault may be contested: " + " | ".join(brief.contest_reasons))
    lines.append(f"Complaint (sanitised, untrusted): {brief.sanitized_complaint[:700]}")
    return "\n".join(lines)


def derive_queries(retrieved: dict[str, dict[str, Any]]) -> list[str]:
    """Step 3 of the method: follow-up queries built from what has been read."""
    cues = [
        (r"Board approved policy|bank's policy", "customer liability as per the bank's Board approved policy beyond seven working days"),
        (r"Table 1", "maximum liability of a customer by type of account Table 1"),
        (r"Table 2|working days", "number of working days counted from receiving the communication from the bank"),
        (r"shared the payment credentials|negligence", "loss due to negligence by a customer shared payment credentials OTP PIN"),
        (r"third party breach|elsewhere in the system", "third party breach deficiency lies elsewhere in the system notified within three working days"),
        (r"shadow reversal|credit .* within 10 working days", "credit shadow reversal within 10 working days of notification"),
        (r"burden of prov", "burden of proving customer liability lies on the bank"),
    ]
    text = " ".join(p["text"] for p in retrieved.values())
    out = []
    for rx, q in cues:
        if re.search(rx, text, re.I):
            out.append(q)
    return out


def passages_block(retrieved: dict[str, dict[str, Any]], priority: list[str], limit: int = 10) -> str:
    order = list(dict.fromkeys([p for p in priority if p in retrieved] + list(retrieved)))[:limit]
    return "\n".join(f"{cid} | {retrieved[cid]['label']} | {retrieved[cid]['text'][:900]}" for cid in order)


# ===================================================================== graph
def build_graph(ctx: RunContext, tools: Tools):
    s = get_settings()

    def add_passages(state: LState, ps: list[dict[str, Any]], via: str) -> dict[str, dict[str, Any]]:
        retrieved = dict(state.get("retrieved", {}))
        for p in ps:
            if p["chunk_id"] not in retrieved:
                retrieved[p["chunk_id"]] = {**p, "via": via}
        return retrieved

    def run_tool(state: LState, name: str, args: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], float, str]:
        fn = getattr(tools, name)
        res = ctx.tool(name, fn, args, summarise=_summ)
        ps = _passages_from(res)
        md = float(res.get("max_dense_score", 0.0)) if isinstance(res, dict) and "max_dense_score" in res else 0.0
        note = f"{name}({json.dumps(args)[:120]}) -> " + (", ".join(p["chunk_id"] for p in ps[:8]) if ps else json.dumps(res)[:200])
        return add_passages(state, ps, name), md, note

    def required_families(state: LState) -> list[str]:
        return state["rule"]["required"]

    def gaps(state: LState) -> list[str]:
        ids = set(state.get("retrieved", {}))
        return [k for k in required_families(state) if not tools.covered(k, state.get("retrieved", {}))]

    # ------------------------------------------------------------ nodes
    def plan(state: LState) -> LState:
        with ctx.step("plan"):
            ctx.budget.steps += 1
            if state.get("degraded"):
                return {"action": {"action": "finish"}}
            p = prompts.get("liability.plan")
            retrieved = state.get("retrieved", {})
            listing = "\n".join(f"- {cid}: {r['label']} - {r['text'][:110]}" for cid, r in list(retrieved.items())[-14:]) or "(nothing yet)"
            g = gaps(state)
            try:
                out = ctx.llm(p, "plan", case=case_text(tools.brief, tools.request), retrieved=listing,
                              catalogue=tools.catalogue(),
                              gaps="\n".join(f"- {R.family_gap_text(k)}" for k in g) or "(none)",
                              strategy=STRATEGIES[state.get("strategy_idx", 0)], budget=ctx.budget.describe(),
                              history="; ".join(state.get("history", [])[-6:]) or "(none)")
            except (LLMUnavailable, BudgetExhausted) as exc:
                ctx.event("fallback", step="plan", reason=str(exc)[:200], using="deterministic research")
                M.DEGRADED.labels(AGENT).inc()
                return {"action": {"action": "finish"}, "degraded": isinstance(exc, LLMUnavailable)}
            except ValueError:
                M.AGENT_RETRIES.labels(AGENT, "invalid_output").inc()
                out = {"action": "search_corpus", "args": {"query": R.family_gap_text(g[0]) if g else "customer liability unauthorised electronic banking transaction"}}
            action = out.get("action") or "finish"
            if action not in ("search_corpus", "get_clause", "get_related_provisions", "lookup_request_fields", "finish"):
                action = "finish"
            return {"action": {"action": action, "args": out.get("args") or {}, "thought": str(out.get("thought", ""))[:300]},
                    "phase_turns": state.get("phase_turns", 0) + 1}

    def act(state: LState) -> LState:
        with ctx.step("act"):
            a = state["action"]
            name, args = a["action"], dict(a.get("args") or {})
            allowed = {"search_corpus": {"query"}, "get_clause": {"clause_id"},
                       "get_related_provisions": {"clause_id"}, "lookup_request_fields": {"fields"}}[name]
            args = {k: v for k, v in args.items() if k in allowed}
            history = list(state.get("history", []))
            sig = f"{name}({json.dumps(args)[:120]})"
            if any(h.startswith(sig) for h in history):
                history.append(f"{sig} -> duplicate call skipped; choose a different tool or arguments")
                M.AGENT_RETRIES.labels(AGENT, "duplicate_tool_call").inc()
                dups = sum("duplicate call skipped" in h for h in history)
                return {"history": history, "stop_reason": "stuck" if dups >= 2 else ""}
            try:
                retrieved, md, note = run_tool(state, name, args)
            except (ToolError, TypeError) as exc:
                history.append(f"{name} failed: {str(exc)[:80]}")
                return {"history": history}
            except BudgetExhausted:
                return {"history": history, "stop_reason": "tool budget exhausted"}
            history.append(note)
            return {"retrieved": retrieved, "history": history, "max_dense": max(state.get("max_dense", 0.0), md)}

    def after_plan(state: LState) -> str:
        a = state.get("action", {})
        if a.get("action", "finish") == "finish" or state.get("stop_reason"):
            return "coverage"
        if not ctx.budget.can_call_tool() or not ctx.budget.can_step():
            return "coverage"
        return "act"

    def after_act(state: LState) -> str:
        phase_cap = 5 if state.get("strategy_idx", 0) == 0 else 3
        if state.get("stop_reason") or not ctx.budget.can_step() or state.get("phase_turns", 0) >= phase_cap:
            return "coverage"
        return "plan"

    def coverage(state: LState) -> LState:
        """Modifier/coverage check. Re-plan once with the gaps shown; otherwise run
        the current deterministic strategy to close them."""
        with ctx.step("coverage"):
            g = gaps(state)
            ctx.event("coverage", gaps=g, retrieved=list(state.get("retrieved", {})), strategy=STRATEGIES[state.get("strategy_idx", 0)])
            if not g:
                return {"stop_reason": ""}
            if not state.get("degraded") and state.get("coverage_rounds", 0) == 0 and ctx.budget.can_step() and ctx.budget.can_call_tool():
                return {"coverage_rounds": 1, "phase_turns": 0, "stop_reason": "replan_gaps"}
            return deterministic_research(state, g)

    def deterministic_research(state: LState, g: list[str]) -> LState:
        upd: LState = {"stop_reason": ""}
        cur = dict(state)
        history = list(state.get("history", []))
        # (a) queries for the missing provisions, then queries derived from passage text already read
        queries = [R.family_gap_text(k) for k in gaps(cur)] + derive_queries(cur.get("retrieved", {}))
        for q in dict.fromkeys(queries):
            if not gaps(cur) or not ctx.budget.can_call_tool():
                break
            try:
                retrieved, md, note = run_tool(cur, "search_corpus", {"query": q, "mode": "bm25" if STRATEGIES[cur.get("strategy_idx", 0)] == "targeted" else "hybrid"})
                cur["retrieved"] = retrieved
                cur["max_dense"] = max(cur.get("max_dense", 0.0), md)
                history.append(note)
            except (ToolError, BudgetExhausted):
                break
        # (b) expand through the clause graph from the passages that cover a required family
        seeds = [cid for k in required_families(cur) for cid in tools.covered(k, cur.get("retrieved", {}))][:3]
        for cid in seeds:
            if not gaps(cur) or not ctx.budget.can_call_tool():
                break
            try:
                retrieved, _, note = run_tool(cur, "get_related_provisions", {"clause_id": cid})
                cur["retrieved"] = retrieved
                history.append(note)
            except (ToolError, BudgetExhausted):
                break
        upd.update({"retrieved": cur.get("retrieved", {}), "history": history, "max_dense": cur.get("max_dense", 0.0)})
        return upd

    def after_coverage(state: LState) -> str:
        return "plan" if state.get("stop_reason") == "replan_gaps" else "draft"

    def draft(state: LState) -> LState:
        with ctx.step("draft"):
            if state.get("degraded"):
                return {"draft": {}}
            p = prompts.get("liability.draft", state.get("variant"))
            priority = [cid for k in required_families(state) for cid in tools.covered(k, state.get("retrieved", {}))]
            try:
                out = ctx.llm(p, "draft", case=case_text(tools.brief, tools.request),
                              passages=passages_block(state.get("retrieved", {}), priority),
                              feedback=state.get("feedback", ""))
            except (LLMUnavailable, BudgetExhausted) as exc:
                ctx.event("fallback", step="draft", reason=str(exc)[:200], using="degraded escalation")
                M.DEGRADED.labels(AGENT).inc()
                return {"draft": {}, "degraded": True}
            except ValueError as exc:
                return {"draft": {"_invalid": str(exc)}}
            upd: LState = {"draft": out}
            if "first_draft" not in state:
                upd["first_draft"] = out
            return upd

    def verify(state: LState) -> LState:
        with ctx.step("verify"):
            if state.get("degraded"):
                return {"verification": {"passed": False, "failures": ["degraded: no model draft"], "checks": {}}}
            rep = verify_draft(ctx, tools, state)
            attempts = state.get("attempts", 0) + 1
            M.VERIFY.labels("pass" if rep["passed"] else "fail").inc()
            ctx.event("verification", attempt=attempts, **rep)
            return {"verification": rep, "attempts": attempts}

    def after_verify(state: LState) -> str:
        if state.get("degraded") or state["verification"]["passed"]:
            return "finalize"
        if state["attempts"] >= s.agent_max_verification_attempts or ctx.budget.remaining_s() < 10:
            return "finalize"
        return "replan"

    def replan(state: LState) -> LState:
        with ctx.step("replan"):
            M.AGENT_RETRIES.labels(AGENT, "verification").inc()
            idx = min(state.get("strategy_idx", 0) + 1, len(STRATEGIES) - 1)
            fails = state["verification"]["failures"]
            ctx.event("retry", target="verification", strategy=STRATEGIES[idx], failures=fails)
            cur = {**state, "strategy_idx": idx}
            g = gaps(cur) or [k for k in required_families(cur)]
            upd = deterministic_research(cur, g)
            # unsupported claims: search for what they assert, so the redraft can ground or drop them
            for f in fails:
                m = re.match(r"unsupported claim: (.+)", f)
                if m and ctx.budget.can_call_tool():
                    try:
                        retrieved, _, note = run_tool({**cur, **upd}, "search_corpus", {"query": m.group(1)[:200]})
                        upd["retrieved"] = retrieved
                        upd["history"] = upd.get("history", []) + [note]
                    except (ToolError, BudgetExhausted):
                        pass
            feedback = ("Your previous draft FAILED verification for these reasons:\n- " + "\n- ".join(fails) +
                        "\nRedraft using only the passages listed; cite chunk ids exactly as listed; drop any claim "
                        "you cannot support; follow the decision procedure.")
            return {**upd, "strategy_idx": idx, "feedback": feedback}

    def finalize(state: LState) -> LState:
        with ctx.step("finalize"):
            return {"final": compose(ctx, tools, state).model_dump(mode="json")}

    g = StateGraph(LState)
    for name, fn in [("plan", plan), ("act", act), ("coverage", coverage), ("draft", draft), ("verify", verify),
                     ("replan", replan), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.set_entry_point("plan")
    g.add_conditional_edges("plan", after_plan, {"act": "act", "coverage": "coverage"})
    g.add_conditional_edges("act", after_act, {"plan": "plan", "coverage": "coverage"})
    g.add_conditional_edges("coverage", after_coverage, {"plan": "plan", "draft": "draft"})
    g.add_edge("draft", "verify")
    g.add_conditional_edges("verify", after_verify, {"finalize": "finalize", "replan": "replan"})
    g.add_edge("replan", "draft")
    g.add_edge("finalize", END)
    return g.compile()


# ============================================================== verification
def verify_draft(ctx: RunContext, tools: Tools, state: LState) -> dict[str, Any]:
    s = get_settings()
    d = state.get("draft") or {}
    retrieved = state.get("retrieved", {})
    rule = state["rule"]
    failures: list[str] = []
    checks: dict[str, Any] = {}
    if not d or "_invalid" in d:
        return {"passed": False, "failures": ["draft is not valid JSON"], "checks": {}}
    route, band = str(d.get("route", "")), str(d.get("liability_band", ""))
    checks["schema"] = route in Route.__members__ and band in LiabilityBand.__members__
    if not checks["schema"]:
        failures.append(f"invalid route/band: {route}/{band}")
    claims = [c for c in (d.get("claims") or []) if isinstance(c, dict) and c.get("text")]
    for c in claims:  # tolerate "A and B" / "A, B" inside one citation string; ids themselves are not altered
        cites = c.get("citations") or []
        cites = [cites] if isinstance(cites, str) else cites
        c["citations"] = [x for raw in cites for x in re.split(r"\s*(?:,|;|\band\b)\s*", str(raw).strip()) if x]
    if not claims:
        failures.append("no claims with citations")
    # V2: every citation resolves to retrieved text
    unresolved = sorted({str(c) for cl in claims for c in (cl.get("citations") or []) if str(c) not in retrieved})
    checks["unresolved_citations"] = unresolved
    if unresolved:
        failures.append("citations do not resolve to retrieved passages: " + ", ".join(unresolved[:5]))
    # V3: each claim supported by its cited passages
    tx = tools.request["transaction"]
    allowed = [str(int(tx["amount_inr"])), str(tools.brief.reporting_delay.working_days), str(tools.brief.reporting_delay.calendar_days)]
    pairs, idx = [], []
    for i, cl in enumerate(claims):
        cited = [retrieved[c]["text"] for c in (cl.get("citations") or []) if c in retrieved]
        if cited:
            pairs.append({"claim": str(cl["text"])[:1500], "passage": " ".join(cited)[:7900], "allowed_numbers": allowed})
            idx.append(i)
    scores = []
    if pairs:
        scores = post_json(ctx.http, f"{s.retriever_url}/v1/support", {"pairs": pairs}, timeout_s=s.tool_timeout_s, retries=1)["scores"]
    claim_results = []
    for i, cl in enumerate(claims):
        sc = scores[idx.index(i)] if i in idx else None
        ok = bool(sc) and (sc["cosine"] >= s.support_threshold or (sc["cosine"] >= s.support_threshold - 0.1 and sc["token_recall"] >= 0.5)) and sc["numbers_consistent"]
        claim_results.append({"text": cl["text"], "citations": cl.get("citations") or [], "supported": ok,
                              "support": sc})
        if not ok:
            failures.append(f"unsupported claim: {str(cl['text'])[:160]}")
    checks["claims"] = claim_results
    # V4: governing provisions cited (core families) and retrieved
    cited_ids = {c for cl in claims for c in (cl.get("citations") or []) if c in retrieved}
    core = [k for k in rule["required"] if k not in SUPPORTING]
    cited_passages = {c: retrieved[c] for c in cited_ids}
    missing_cited = [k for k in core if not tools.covered(k, cited_passages)]
    missing_retrieved = [k for k in rule["required"] if not tools.covered(k, retrieved)]
    checks["missing_core_citations"] = missing_cited
    checks["missing_retrieved_families"] = missing_retrieved
    for k in missing_cited:
        failures.append(f"governing provision not cited: {R.family_gap_text(k)}")
    # V5: consistency with the liability table
    rule_route = rule["route"]
    if rule_route == Route.ESCALATE_TO_ANALYST.value:
        checks["consistent"] = route == rule_route
        if route != rule_route:
            failures.append(f"case requires escalation ({', '.join(rule['escalate'])}) but draft routes to {route}")
    else:
        checks["consistent"] = route == rule_route and band == rule["band"]
        if not checks["consistent"]:
            failures.append(f"draft {band}/{route} disagrees with liability table {rule['band']}/{rule['route']}")
    # V6: authorisation filter on generated text
    _, viol = G.filter_output(" ".join([str(d.get("summary", "")), str(d.get("basis", ""))] + [c["text"] for c in claims]))
    checks["output_violations"] = viol
    return {"passed": not failures, "failures": failures, "checks": checks}


# ================================================================ compose
def _citation(r: dict[str, Any]) -> Citation:
    return Citation(chunk_id=r["chunk_id"], clause_id=r["clause_id"], label=r["label"], quote=r["text"][:600],
                    source_url=r.get("source_url", ""))


def compose(ctx: RunContext, tools: Tools, state: LState) -> Recommendation:
    brief, rule = tools.brief, state["rule"]
    retrieved = state.get("retrieved", {})
    ver = state.get("verification") or {"passed": False, "failures": [], "checks": {}}
    draft = state.get("draft") or {}
    reasons = [EscalationReason(e) for e in rule["escalate"]]
    if brief.guardrails.complaint_withheld_from_model:
        reasons.append(EscalationReason.GUARDRAIL_TRIGGERED)
    degraded = bool(state.get("degraded") or brief.degraded)
    if degraded:
        reasons.append(EscalationReason.DEGRADED_MODE)
    if state.get("max_dense", 1.0) and state.get("max_dense", 1.0) < get_settings().out_of_corpus_threshold:
        reasons.append(EscalationReason.OUT_OF_CORPUS)

    # supporting provisions: everything the rule check needs that was actually retrieved
    family_ids = list(dict.fromkeys(cid for k in rule["required"] for cid in tools.covered(k, retrieved)))
    if ver.get("passed"):
        claims = [Claim(text=c["text"], citations=c["citations"], supported=True, support_score=(c.get("support") or {}).get("cosine"))
                  for c in ver["checks"]["claims"]]
        route = Route(draft["route"])
        band = LiabilityBand(draft["liability_band"])
        summary, basis = str(draft.get("summary", "")), str(draft.get("basis", ""))
    else:
        supported = [c for c in (ver.get("checks", {}).get("claims") or []) if c["supported"]]
        claims = [Claim(text=c["text"], citations=c["citations"], supported=True, support_score=(c.get("support") or {}).get("cosine")) for c in supported]
        if not claims:  # quote the governing provisions verbatim rather than paraphrase without verification
            claims = [Claim(text=retrieved[cid]["text"][:400], citations=[cid], supported=True, support_score=1.0)
                      for cid in family_ids[:4]]
        if not degraded:
            reasons.append(EscalationReason.VERIFICATION_FAILED if ver.get("failures") else EscalationReason.BUDGET_EXHAUSTED)
        route, band = Route.ESCALATE_TO_ANALYST, LiabilityBand.UNDETERMINED
        suggestion = (f"Unverified rule-check suggestion: fault {rule['fault']}, band {rule['band']}, route {rule['route']}.")
        summary = (("The model was unavailable; " if degraded else "The drafted position could not be verified against the retrieved provisions; ")
                   + "no recommendation is made. " + suggestion)
        basis = "Provisions retrieved for the analyst: " + ", ".join(dict.fromkeys(retrieved[c]["label"] for c in family_ids)) if family_ids else "No governing provision could be verified."
    if reasons:
        route = Route.ESCALATE_TO_ANALYST
        if band not in (LiabilityBand.UNDETERMINED,):
            band = LiabilityBand.UNDETERMINED if EscalationReason.FAULT_CONTESTED in reasons or EscalationReason.OUT_OF_CORPUS in reasons else band
        if rule.get("alternative"):
            summary = summary + " Competing outcomes: " + json.dumps(rule["alternative"])
    reasons = list(dict.fromkeys(reasons))

    cited = list(dict.fromkeys([c for cl in claims for c in cl.citations] + family_ids))
    citations = [_citation(retrieved[c]) for c in cited if c in retrieved]
    summary, v1 = G.filter_output(summary)
    basis, v2 = G.filter_output(basis)
    for cl in claims:
        cl.text, v3 = G.filter_output(cl.text)
        v1 += v3
    viol = v1 + v2
    for v in viol:
        M.GUARDRAIL.labels("output", v).inc()
    if viol:
        ctx.event("guardrail", guard="output", violations=viol)
    rec = Recommendation(
        run_id=ctx.run_id, completed=True, route=route, liability_band=band,
        customer_liability_inr=rule.get("customer_liability_inr") if route != Route.ESCALATE_TO_ANALYST else None,
        category=brief.category, fault_attribution=Fault(rule["fault"]),
        reporting_delay_working_days=brief.reporting_delay.working_days, summary=summary, basis=basis,
        claims=claims, citations=citations, escalation_reasons=reasons,
        escalation_path=ESCALATION_PATH if route == Route.ESCALATE_TO_ANALYST else
        "Analyst reviews and confirms; customer may approach the RBI Ombudsman if dissatisfied.",
        verification=VerificationReport(passed=bool(ver.get("passed")), attempts=state.get("attempts", 0),
                                        checks={k: v for k, v in ver.get("checks", {}).items() if k != "claims"},
                                        failures=ver.get("failures", [])),
        degraded=degraded, prompt_versions={**brief.prompt_versions, **ctx.prompt_versions},
    )
    return rec


def run_liability(ctx: RunContext, request: dict[str, Any], brief: TriageBrief, profile: str, variant: str) -> tuple[Recommendation, dict[str, Any]]:
    tx, cu = request["transaction"], request["customer"]
    rule = R.evaluate(brief.category, brief.fault_hypothesis, brief.reporting_delay.working_days, tx["amount_inr"],
                      cu["account_type"], cu.get("credit_limit_inr"), tx["channel"])
    ctx.event("rule_check", **rule.as_dict())
    tools = Tools(ctx, profile, request, brief)
    graph = build_graph(ctx, tools)
    init: LState = {"brief": brief.model_dump(mode="json"), "request": request, "profile": profile, "variant": variant,
                    "rule": rule.as_dict(), "retrieved": {}, "history": [], "strategy_idx": 0, "phase_turns": 0,
                    "coverage_rounds": 0, "attempts": 0, "degraded": brief.degraded}
    final = graph.invoke(init, config={"recursion_limit": 60})
    rec = Recommendation(**final["final"])
    rec.cost_usd = round(ctx.cost_usd, 8)
    rec.tokens = dict(ctx.tokens)
    return rec, final
