"""Locust load test (D6). Measures a COMPLETE run: submission -> polling -> final
result, reported as the custom request type "RUN" / name "complete_run".

Run with the LLM replaced by llm-stub (LLM_BASE_URL=http://llm-stub:8090/v1),
so the test measures this system, not a provider's rate limits.

    locust -f loadtest/locustfile.py --host http://localhost:8080 --headless -u 10 -r 2 -t 3m --csv loadtest/results/u10
"""
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from locust import HttpUser, between, events, task

POLL_S = 1.0
RUN_TIMEOUT_S = 300

COMPLAINTS = [
    ("CARD_ONLINE", "CVV_ONLY", "An online purchase I did not make appeared on my card. The card is with me and I shared nothing."),
    ("CARD_ATM", "PIN", "Cash was withdrawn from an ATM in another city while my card was with me. I think it was skimmed."),
    ("CARD_POS", "MAGSTRIPE", "Two swipe transactions at a shop I never visited. My card never left my wallet."),
]


def make_request() -> dict:
    ch, auth, text = random.choice(COMPLAINTS)
    now = datetime.now(timezone.utc)
    days = random.choice([1, 2, 5, 9])
    tx_at = now - timedelta(days=days, hours=2)
    return {
        "client_reference": "lt-" + uuid.uuid4().hex[:20],
        "customer": {"customer_id": f"CUST-{random.randint(0, 999999):06d}", "account_type": "SAVINGS"},
        "transaction": {"transaction_id": "TXN-LT" + uuid.uuid4().hex[:10].upper(), "channel": ch,
                        "amount_inr": random.choice([2500, 9000, 18000, 42000]), "transaction_at": tx_at.isoformat(),
                        "card_last4": f"{random.randint(0, 9999):04d}", "auth_method": auth},
        "reporting": {"alert_received_at": tx_at.isoformat(), "reported_at": (now - timedelta(hours=1)).isoformat(),
                      "reporting_channel": "PHONE"},
        "complaint_text": text + f" Reference {uuid.uuid4().hex[:8]}.",
        "customer_assertions": {"card_in_possession": "YES", "credentials_shared": "NO"},
    }


class DisputeClient(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.client_id = "lt-" + uuid.uuid4().hex[:8]  # one rate-limit bucket per simulated client

    @task
    def complete_run(self):
        t0 = time.perf_counter()
        with self.client.post("/v1/disputes", json=make_request(), headers={"X-Client-ID": self.client_id},
                              name="POST /v1/disputes", catch_response=True) as r:
            if r.status_code != 202:
                r.failure(f"submit {r.status_code}")
                events.request.fire(request_type="RUN", name="complete_run", response_time=(time.perf_counter() - t0) * 1000,
                                    response_length=0, exception=Exception(f"submit {r.status_code}"))
                return
            run_id = r.json()["run_id"]
        status = "queued"
        while time.perf_counter() - t0 < RUN_TIMEOUT_S:
            time.sleep(POLL_S)
            p = self.client.get(f"/v1/runs/{run_id}", name="GET /v1/runs/{id}")
            if p.status_code == 200:
                status = p.json()["status"]
                if status in ("completed", "failed"):
                    break
        elapsed = (time.perf_counter() - t0) * 1000
        exc = None if status == "completed" else Exception(f"run {status}")
        events.request.fire(request_type="RUN", name="complete_run", response_time=elapsed, response_length=0, exception=exc)
