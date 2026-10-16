"""Batch evaluation from the command line (L2, L3).

    python eval/run_eval.py --label v2-clause --prompt-variant v2 --retrieval-profile clause
    python eval/run_eval.py --label v1-clause --prompt-variant v1
    python eval/run_eval.py --label v2-naive  --retrieval-profile naive
    python eval/run_eval.py --compare v1-clause v2-clause

Runs every scenario in eval/scenarios.yaml end to end through the public API
(so validation, queue, both agents, guardrails and verification are all in the
loop), fetches each run's trace, and scores it with triage.evaluation.metrics.
The API must have ALLOW_EXPERIMENT_OVERRIDES=true for the variant flags.

Outputs eval/results/<label>.json (per-scenario rows) and <label>.md.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from triage.agents.workdays import is_working_day  # noqa: E402
from triage.common.http import UpstreamError  # noqa: E402
from triage.evaluation.metrics import score_run  # noqa: E402

API = os.environ.get("API_URL", "http://localhost:8080")
RETRIEVER = os.environ.get("RETRIEVER_URL_HOST", os.environ.get("EVAL_RETRIEVER_URL", "http://localhost:8081"))
RESULTS = ROOT / "eval" / "results"


def dates_for(working_days: int) -> tuple[datetime, datetime]:
    """Report on the most recent working day before today; alert `working_days` working days earlier."""
    rep = datetime.now(timezone.utc).replace(hour=11, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while not is_working_day(rep.date()):
        rep -= timedelta(days=1)
    alert = rep
    n = 0
    while n < working_days:
        if is_working_day(alert.date()):
            n += 1
        alert -= timedelta(days=1)
    alert = alert.replace(hour=9)
    # alert must be a day d such that working days in (d, rep] == working_days
    return alert, rep


def build_request(sc: dict, label: str, variant: str | None, profile: str | None) -> dict:
    alert, rep = dates_for(sc["working_days"])
    txn_at = alert - timedelta(minutes=5)
    tx = {"transaction_id": "TXN-" + sc["id"].split("_")[0] + "X" + str(int(time.time()) % 100000).zfill(5),
          "channel": sc["channel"], "amount_inr": sc["amount"], "transaction_at": txn_at.isoformat(),
          "auth_method": sc.get("auth_method", "UNKNOWN"), "merchant_name": sc.get("merchant")}
    if sc["channel"].startswith("CARD_"):
        tx["card_last4"] = "4242"
    if sc.get("duplicate_of"):
        tx["duplicate_of_transaction_id"] = sc["duplicate_of"]
    req = {
        "client_reference": f"eval-{label}-{sc['id']}-{int(time.time() * 1000) % 10**9}"[:64],
        "customer": {"customer_id": "CUST-" + str(abs(hash(sc["id"])) % 10**6).zfill(6), "account_type": sc["account_type"]},
        "transaction": tx,
        "reporting": {"reported_at": rep.isoformat(), "reporting_channel": "PHONE"},
        "complaint_text": sc["complaint"],
        "customer_assertions": sc.get("assertions", {}),
    }
    if sc.get("credit_limit"):
        req["customer"]["credit_limit_inr"] = sc["credit_limit"]
    if sc.get("alert", True):
        req["reporting"]["alert_received_at"] = alert.isoformat()
    exp = {k: v for k, v in {"prompt_variant": variant, "retrieval_profile": profile}.items() if v}
    if exp:
        req["experiment"] = exp
    return req


def run_one(client: httpx.Client, sc: dict, label: str, variant: str | None, profile: str | None, timeout_s: float) -> dict:
    req = build_request(sc, label, variant, profile)
    t0 = time.time()
    r = client.post(f"{API}/v1/disputes", json=req, headers={"X-Client-ID": "eval-runner"})
    if r.status_code not in (200, 202):
        return {"id": sc["id"], "error": f"submit {r.status_code}: {r.text[:300]}"}
    run_id = r.json()["run_id"]
    while time.time() - t0 < timeout_s:
        j = client.get(f"{API}/v1/runs/{run_id}").json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(2)
    else:
        return {"id": sc["id"], "run_id": run_id, "error": "timeout"}
    latency = time.time() - t0
    trace = client.get(f"{API}/v1/runs/{run_id}/trace").json()
    return score(client, sc, run_id, j["result"], trace, latency)


def _first_draft(trace: dict) -> dict | None:
    from triage.agents.context import parse_json_object

    for e in trace["events"]:
        if e["kind"] == "llm_call" and e.get("step") == "draft" and e.get("output"):
            return parse_json_object(e["output"])
    return None


def score(client: httpx.Client, sc: dict, run_id: str, res: dict, trace: dict, latency: float) -> dict:
    exp = sc["expected"]
    passages = {cid: p["text"] for cid, p in trace["summary"]["passages"].items()}
    brief = trace.get("brief") or {}
    case = sc["complaint"] + f" Channel {sc['channel']}, delay {sc['working_days']} working days."
    allowed = [str(sc["amount"]), str(sc["working_days"])]
    try:
        final_m = score_run(client, RETRIEVER, case, res.get("claims", []), passages, allowed)
    except UpstreamError as exc:
        final_m = {"error": str(exc)}
    fd = _first_draft(trace)
    draft_m = {}
    if fd and isinstance(fd.get("claims"), list):
        claims = []
        for c in fd["claims"]:
            if isinstance(c, dict) and c.get("text"):
                cites = c.get("citations") or []
                claims.append({"text": str(c["text"]), "citations": [cites] if isinstance(cites, str) else [str(x) for x in cites]})
        try:
            draft_m = score_run(client, RETRIEVER, case, claims, passages, allowed)
        except UpstreamError:
            draft_m = {}
    gold = sc.get("gold", [])
    cited_texts = {c["chunk_id"]: c["quote"] for c in res.get("citations", [])}
    retrieved_hit = [g for g in gold if gold_in(client, g, passages)]
    cited_hit = [g for g in gold if gold_in(client, g, cited_texts)]
    route_ok = res["route"] == exp["route"]
    band_ok = exp.get("band") is None or res["liability_band"] == exp["band"]
    liab_ok = "liability_inr" not in exp or (res.get("customer_liability_inr") is not None and abs(res["customer_liability_inr"] - exp["liability_inr"]) < 1)
    reasons_ok = set(exp.get("escalation_reasons", [])) <= set(res.get("escalation_reasons", []))
    s = trace["summary"]
    return {
        "id": sc["id"], "run_id": run_id, "expected_route": exp["route"], "route": res["route"],
        "band": res["liability_band"], "liability_inr": res.get("customer_liability_inr"),
        "escalation_reasons": res.get("escalation_reasons", []), "category": res.get("category"),
        "route_correct": route_ok, "outcome_correct": route_ok and band_ok and liab_ok and reasons_ok,
        "should_escalate": exp["route"] == "ESCALATE_TO_ANALYST", "escalated": res["route"] == "ESCALATE_TO_ANALYST",
        "gold_retrieved": (bool(retrieved_hit) if gold else None),
        "gold_cited": (bool(cited_hit) if gold else None),
        "verification_passed": (res.get("verification") or {}).get("passed"),
        "verification_attempts": (res.get("verification") or {}).get("attempts"),
        "degraded": res.get("degraded"),
        "groundedness": final_m.get("groundedness"), "faithfulness": final_m.get("faithfulness"),
        "context_relevance": final_m.get("context_relevance"), "hallucinated": final_m.get("hallucinated"),
        "draft_groundedness": draft_m.get("groundedness"), "draft_faithfulness": draft_m.get("faithfulness"),
        "draft_hallucinated": draft_m.get("hallucinated"),
        "n_claims": final_m.get("n_claims"), "tool_calls": len(s["tool_calls"]), "llm_calls": len(s["llm_calls"]),
        "prompt_tokens": sum(int(x.get("prompt_tokens") or 0) for x in s["llm_calls"]),
        "completion_tokens": sum(int(x.get("completion_tokens") or 0) for x in s["llm_calls"]),
        "cost_usd": s["cost_usd_total"], "cost_by_step": s["cost_by_step"], "latency_s": round(latency, 2),
        "prompt_versions": s["prompt_versions"],
    }


_GOLD: dict[str, list[str]] = {}


def _alnum(t: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", t.lower())


def gold_in(client: httpx.Client, gold_id: str, passages: dict[str, str]) -> bool:
    """True if the gold clause is among `passages`, by id (clause chunking) or by content (any chunking)."""
    if any(cid == gold_id or cid.split("~")[0] == gold_id for cid in passages):
        return True
    if gold_id not in _GOLD:
        try:
            t = _alnum(client.get(f"{RETRIEVER}/v1/clause/{gold_id}?profile=clause").json()["chunks"][0]["text"])
            _GOLD[gold_id] = [t[i:i + 40] for i in (8, len(t) // 2, max(8, len(t) - 60)) if len(t) > i + 40]
        except Exception:
            _GOLD[gold_id] = []
    sigs = _GOLD[gold_id]
    return any(any(sg in _alnum(p) for sg in sigs) for p in passages.values())


def mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def aggregate(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    esc_true = [r for r in ok if r["should_escalate"]]
    esc_pred = [r for r in ok if r["escalated"]]
    lat = sorted(r["latency_s"] for r in ok)
    return {
        "n": len(rows), "errors": len(rows) - len(ok),
        "route_accuracy": mean([float(r["route_correct"]) for r in ok]),
        "outcome_accuracy": mean([float(r["outcome_correct"]) for r in ok]),
        "escalation_recall": mean([float(r["escalated"]) for r in esc_true]),
        "escalation_precision": mean([float(r["should_escalate"]) for r in esc_pred]),
        "gold_recall": mean([float(r["gold_retrieved"]) for r in ok if r["gold_retrieved"] is not None]),
        "gold_cited": mean([float(r["gold_cited"]) for r in ok if r["gold_cited"] is not None]),
        "groundedness": mean([r["groundedness"] for r in ok]),
        "faithfulness": mean([r["faithfulness"] for r in ok]),
        "context_relevance": mean([r["context_relevance"] for r in ok]),
        "hallucination_rate": mean([r["hallucinated"] for r in ok]),
        "draft_groundedness": mean([r["draft_groundedness"] for r in ok]),
        "draft_faithfulness": mean([r["draft_faithfulness"] for r in ok]),
        "draft_hallucination_rate": mean([r["draft_hallucinated"] for r in ok]),
        "verification_pass_rate": mean([float(bool(r["verification_passed"])) for r in ok]),
        "mean_tool_calls": mean([r["tool_calls"] for r in ok]), "mean_llm_calls": mean([r["llm_calls"] for r in ok]),
        "mean_prompt_tokens": mean([r["prompt_tokens"] for r in ok]),
        "mean_completion_tokens": mean([r["completion_tokens"] for r in ok]),
        "cost_per_100_runs_usd": round(100 * sum(r["cost_usd"] for r in ok) / max(1, len(ok)), 6),
        "latency_p50_s": lat[len(lat) // 2] if lat else None,
        "latency_p95_s": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None,
    }


def write_report(label: str, meta: dict, rows: list[dict], agg: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{label}.json").write_text(json.dumps({"meta": meta, "aggregate": agg, "rows": rows}, indent=1))
    lines = [f"# Evaluation: {label}", "", f"Config: `{json.dumps(meta)}`", "", "## Aggregate", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in agg.items()]
    lines += ["", "## Scenarios", "", "| id | expected | got | band | outcome ok | verified | grounded | faithful | halluc. | latency s |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['id']} | error: {r['error'][:60]} |||||||||")
            continue
        lines.append(f"| {r['id']} | {r['expected_route']} | {r['route']} | {r['band']} | {'yes' if r['outcome_correct'] else 'NO'} | "
                     f"{r['verification_passed']} | {r['groundedness']} | {r['faithfulness']} | {r['hallucinated']} | {r['latency_s']} |")
    (RESULTS / f"{label}.md").write_text("\n".join(lines) + "\n")


def compare(a: str, b: str) -> None:
    A = json.loads((RESULTS / f"{a}.json").read_text())["aggregate"]
    B = json.loads((RESULTS / f"{b}.json").read_text())["aggregate"]
    print(f"| metric | {a} | {b} | delta |\n|---|---|---|---|")
    for k in A:
        va, vb = A[k], B.get(k)
        d = round(vb - va, 4) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else ""
        print(f"| {k} | {va} | {vb} | {d} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label")
    ap.add_argument("--prompt-variant")
    ap.add_argument("--retrieval-profile")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--merge", nargs=2, metavar=("BASE", "RERUN"),
                    help="replace BASE rows with RERUN rows of the same id (used after a provider outage)")
    a = ap.parse_args()
    if a.merge:
        base = json.loads((RESULTS / f"{a.merge[0]}.json").read_text())
        rerun = {r["id"]: r for r in json.loads((RESULTS / f"{a.merge[1]}.json").read_text())["rows"]}
        rows = [rerun.get(r["id"], r) for r in base["rows"]]
        base["meta"]["rerun_ids"] = sorted(rerun)
        write_report(a.merge[0], base["meta"], rows, aggregate(rows))
        print(f"merged {len(rerun)} rows into {a.merge[0]}")
        return 0
    if a.compare:
        compare(*a.compare)
        return 0
    scenarios = yaml.safe_load((ROOT / "eval" / "scenarios.yaml").read_text())["scenarios"]
    if a.only:
        scenarios = [s for s in scenarios if s["id"] in a.only]
    label = a.label or f"{a.prompt_variant or 'default'}-{a.retrieval_profile or 'default'}"
    t0 = time.time()
    with httpx.Client(timeout=60) as client, cf.ThreadPoolExecutor(a.concurrency) as ex:
        futs = {ex.submit(run_one, client, sc, label, a.prompt_variant, a.retrieval_profile, a.timeout): sc for sc in scenarios}
        rows = []
        for f in cf.as_completed(futs):
            try:
                r = f.result()
            except Exception as exc:
                r = {"id": futs[f]["id"], "error": f"{type(exc).__name__}: {exc}"}
            rows.append(r)
            print(json.dumps({k: r.get(k) for k in ("id", "expected_route", "route", "band", "outcome_correct", "verification_passed", "groundedness", "latency_s", "error")}))
    rows.sort(key=lambda r: r["id"])
    agg = aggregate(rows)
    meta = {"label": label, "prompt_variant": a.prompt_variant, "retrieval_profile": a.retrieval_profile,
            "started": datetime.fromtimestamp(t0, timezone.utc).isoformat(), "wall_s": round(time.time() - t0, 1)}
    write_report(label, meta, rows, agg)
    print(json.dumps(agg, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
