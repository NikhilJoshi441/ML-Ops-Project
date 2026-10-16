"""Run state, run event log, reliable work queues and quarantine, all in Redis.

Queue semantics (A4): the API pushes a job to `q:triage`; triage-agent replicas
BLMOVE jobs into their own processing list, so a job is never lost if a worker
dies mid-run. Each worker refreshes a heartbeat key; a reaper running in every
worker moves jobs from processing lists of dead workers back to the queue
(attempt + 1). After JOB_MAX_ATTEMPTS the run is closed as INCOMPLETE, which is
reported as an escalation, never as an answer (A5).

Run reconstruction (D5): every step appends an event to `run:<id>:events`; the
API's /v1/runs/<id>/trace returns them in order.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import redis

Q_TRIAGE = "q:triage"
Q_LIABILITY = "q:liability"
QUARANTINE = "quarantine"
QUALITY_SERIES = "quality:series"
FEEDBACK = "feedback:items"


class RunStore:
    def __init__(self, url: str, ttl_s: int = 7 * 24 * 3600):
        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=2,
                                      health_check_interval=30)
        self.ttl = ttl_s

    # ------------------------------------------------------------- runs
    def create_run(self, request: dict[str, Any], client_id: str) -> str:
        run_id = "run_" + uuid.uuid4().hex[:20]
        now = time.time()
        key = f"run:{run_id}"
        pipe = self.r.pipeline()
        pipe.hset(key, mapping={
            "run_id": run_id, "status": "queued", "stage": "submitted", "client_id": client_id,
            "created_at": now, "updated_at": now, "request": json.dumps(request, default=str),
        })
        pipe.expire(key, self.ttl)
        pipe.execute()
        return run_id

    def update(self, run_id: str, **fields: Any) -> None:
        enc = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
        enc["updated_at"] = time.time()
        self.r.hset(f"run:{run_id}", mapping=enc)

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        d = self.r.hgetall(f"run:{run_id}")
        if not d:
            return None
        for k in ("request", "result", "brief", "rule_outcome"):
            if k in d and d[k]:
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        for k in ("created_at", "updated_at", "completed_at"):
            if k in d:
                d[k] = float(d[k])
        return d

    # ----------------------------------------------------------- events
    def event(self, run_id: str, kind: str, agent: str, **data: Any) -> None:
        ev = {"ts": time.time(), "kind": kind, "agent": agent, **data}
        key = f"run:{run_id}:events"
        pipe = self.r.pipeline()
        pipe.rpush(key, json.dumps(ev, default=str))
        pipe.expire(key, self.ttl)
        pipe.execute()

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.r.lrange(f"run:{run_id}:events", 0, -1)]

    # ------------------------------------------------------- idempotency
    def idempotent_run(self, client_id: str, client_ref: str, run_id: str) -> Optional[str]:
        key = f"idem:{client_id}:{client_ref}"
        if self.r.set(key, run_id, nx=True, ex=self.ttl):
            return None
        return self.r.get(key)

    # ------------------------------------------------------------ queues
    def enqueue(self, queue: str, run_id: str, carrier: dict[str, str] | None = None, attempt: int = 1) -> None:
        job = {"run_id": run_id, "attempt": attempt, "carrier": carrier or {}, "enqueued_at": time.time()}
        self.r.lpush(queue, json.dumps(job))

    def dequeue(self, queue: str, worker_id: str, timeout_s: int = 2) -> Optional[tuple[str, dict[str, Any]]]:
        raw = self.r.blmove(queue, f"{queue}:processing:{worker_id}", timeout_s, "RIGHT", "LEFT")
        if raw is None:
            return None
        return raw, json.loads(raw)

    def ack(self, queue: str, worker_id: str, raw: str) -> None:
        self.r.lrem(f"{queue}:processing:{worker_id}", 1, raw)

    def heartbeat(self, worker_id: str, ttl_s: int = 20) -> None:
        self.r.set(f"worker:{worker_id}", time.time(), ex=ttl_s)

    def queue_depth(self, queue: str) -> int:
        return int(self.r.llen(queue))

    def in_flight(self, queue: str) -> int:
        n = 0
        for k in self.r.scan_iter(f"{queue}:processing:*", count=100):
            n += int(self.r.llen(k))
        return n

    def reap(self, queue: str, max_attempts: int, on_give_up) -> int:
        """Requeue jobs held by workers whose heartbeat expired."""
        moved = 0
        for key in self.r.scan_iter(f"{queue}:processing:*", count=100):
            wid = key.rsplit(":", 1)[-1]
            if self.r.exists(f"worker:{wid}"):
                continue
            while True:
                raw = self.r.rpop(key)
                if raw is None:
                    break
                job = json.loads(raw)
                if job.get("attempt", 1) >= max_attempts:
                    on_give_up(job["run_id"], f"worker {wid} died; attempts exhausted")
                else:
                    self.enqueue(queue, job["run_id"], job.get("carrier"), job.get("attempt", 1) + 1)
                moved += 1
        return moved

    # -------------------------------------------------------- quarantine
    def quarantine(self, payload: Any, errors: list[dict[str, Any]], client_id: str) -> str:
        qid = "q_" + uuid.uuid4().hex[:16]
        self.r.xadd(QUARANTINE, {"id": qid, "ts": time.time(), "client_id": client_id,
                                 "errors": json.dumps(errors), "payload": json.dumps(payload, default=str)[:20000]},
                    maxlen=10000, approximate=True)
        return qid

    def quarantined(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for _, f in self.r.xrevrange(QUARANTINE, count=limit):
            out.append({"id": f["id"], "ts": float(f["ts"]), "client_id": f["client_id"],
                        "errors": json.loads(f["errors"]), "payload": json.loads(f["payload"])})
        return out

    # ------------------------------------------------- quality / feedback
    def add_quality(self, run_id: str, metrics: dict[str, Any]) -> None:
        self.r.rpush(QUALITY_SERIES, json.dumps({"ts": time.time(), "run_id": run_id, **metrics}))
        self.r.ltrim(QUALITY_SERIES, -5000, -1)

    def quality_series(self, limit: int = 500) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.r.lrange(QUALITY_SERIES, -limit, -1)]

    def add_feedback(self, run_id: str, item: dict[str, Any]) -> None:
        rec = {"ts": time.time(), "run_id": run_id, **item}
        pipe = self.r.pipeline()
        pipe.rpush(FEEDBACK, json.dumps(rec))
        pipe.hincrby("feedback:counts", "satisfactory" if item["satisfactory"] else "unsatisfactory", 1)
        pipe.hset(f"run:{run_id}", "feedback", json.dumps(rec))
        pipe.execute()

    def feedback(self, limit: int = 1000) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.r.lrange(FEEDBACK, -limit, -1)]
