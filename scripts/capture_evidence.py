"""Runs the demonstration cases against a running stack and stores the responses in docs/evidence/.
Usage: python scripts/capture_evidence.py [api_url] [gateway_url]"""
import json, sys, time, uuid
from pathlib import Path
import httpx

API = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
GW = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8082"
EX, OUT = Path("docs/examples"), Path("docs/evidence")
OUT.mkdir(parents=True, exist_ok=True)
c = httpx.Client(timeout=30)


def submit(name: str) -> str:
    body = json.loads((EX / name).read_text())
    body["client_reference"] = f"ev-{name.split('.')[0][:20]}-{uuid.uuid4().hex[:8]}"
    r = c.post(f"{API}/v1/disputes", json=body, headers={"X-Client-ID": "evidence"})
    r.raise_for_status()
    return r.json()["run_id"]


def wait(run_id: str, timeout=400) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"{API}/v1/runs/{run_id}").json()
        if j["status"] in ("completed", "failed"):
            return j
        time.sleep(2)
    raise TimeoutError(run_id)


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, indent=1, default=str))
    print("wrote", name)


mode = sys.argv[3] if len(sys.argv) > 3 else "all"
if mode in ("all", "cases"):
    runs = {n: submit(n) for n in ("section_4_1.json", "skimming_valid.json", "prompt_injection.json", "out_of_corpus.json")}
    for n, rid in runs.items():
        res = wait(rid)
        tr = c.get(f"{API}/v1/runs/{rid}/trace").json()
        stem = n.split(".")[0]
        save(f"{stem}_result.json", res)
        save(f"{stem}_trace_summary.json", tr["summary"])
        if any(e["kind"] == "verification" and not e.get("passed") for e in tr["events"]):
            save("verification_retry_trace.json", [e for e in tr["events"] if e["kind"] in ("verification", "retry", "tool_call", "coverage", "llm_call")])

if mode in ("all", "breaker"):
    lines = []
    def log(title, obj):
        lines.append(f"### {title}\n{json.dumps(obj, indent=1, default=str)}\n")
    c.post(f"{GW}/admin/breaker/reset")
    log("1. chaos: primary provider marked down", c.post(f"{GW}/admin/chaos", json={"mode": "down"}).json()["primary"])
    rids = [submit("skimming_valid.json") for _ in range(3)]
    results = [wait(r) for r in rids]
    log("2. breaker state after the provider failed", c.get(f"{GW}/admin/breaker").json()["primary"])
    for r in results:
        res = r["result"]
        log(f"3. degraded result {r['run_id']}", {k: res[k] for k in ("completed", "degraded", "route", "liability_band", "escalation_reasons", "summary", "basis")})
    tr = c.get(f"{API}/v1/runs/{rids[-1]}/trace").json()
    log("4. trace events for the last run (llm calls refused fast, fallback taken)",
        [{k: e.get(k) for k in ("kind", "agent", "step", "ok", "error", "using", "reason", "latency_ms")} for e in tr["events"] if e["kind"] in ("llm_call", "fallback", "retry")])
    log("5. chaos cleared", c.post(f"{GW}/admin/chaos", json={"mode": "ok"}).json()["primary"])
    time.sleep(32)
    rid = submit("skimming_valid.json")
    res = wait(rid)["result"]
    log("6. after open period: half-open probe succeeded, breaker closed; normal run", {"breaker": c.get(f"{GW}/admin/breaker").json()["primary"], "route": res["route"], "degraded": res["degraded"]})
    (OUT / "circuit_breaker_demo.txt").write_text("\n".join(lines))
    print("wrote circuit_breaker_demo.txt")
