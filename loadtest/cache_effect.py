"""Measures the effect of caching (D7): the same dispute content submitted N
times (distinct client references), with gateway + retrieval caches on vs off.
Repeated content is realistic: customers resubmit, branches re-key the same
complaint, and analysts re-run a case after editing a field."""
import json, statistics, sys, time, uuid
from datetime import datetime, timedelta, timezone
import httpx

API = "http://localhost:8080"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 8
now = datetime.now(timezone.utc)
base = {
    "customer": {"customer_id": "CUST-123456", "account_type": "SAVINGS"},
    "transaction": {"transaction_id": "TXN-CACHE00001", "channel": "CARD_ONLINE", "amount_inr": 18500,
                    "transaction_at": (now - timedelta(days=2)).replace(microsecond=0).isoformat(), "card_last4": "4242", "auth_method": "CVV_ONLY"},
    "reporting": {"alert_received_at": (now - timedelta(days=2)).replace(microsecond=0).isoformat(),
                  "reported_at": (now - timedelta(days=1)).replace(microsecond=0).isoformat(), "reporting_channel": "PHONE"},
    "complaint_text": "An online purchase of Rs 18,500 that I did not make. My card is with me and I shared nothing.",
    "customer_assertions": {"card_in_possession": "YES", "credentials_shared": "NO"},
}
lat, cost = [], []
with httpx.Client(timeout=30) as c:
    for i in range(N):
        t0 = time.time()
        rid = c.post(f"{API}/v1/disputes", json={**base, "client_reference": "cache-" + uuid.uuid4().hex[:12]},
                     headers={"X-Client-ID": "cache-test"}).json()["run_id"]
        while True:
            j = c.get(f"{API}/v1/runs/{rid}").json()
            if j["status"] in ("completed", "failed"):
                break
            time.sleep(0.25)
        lat.append(time.time() - t0)
        cost.append(j["result"]["cost_usd"])
lat_s = sorted(lat)
out = {"label": sys.argv[1], "n": N, "p50_s": round(statistics.median(lat), 2), "p95_s": round(lat_s[min(N - 1, int(0.95 * N))], 2),
       "mean_cost_usd": round(statistics.mean(cost), 8), "first_run_s": round(lat[0], 2), "latencies": [round(x, 2) for x in lat]}
print(json.dumps(out))
open(f"loadtest/results/cache_{sys.argv[1]}.json", "w").write(json.dumps(out, indent=1))
