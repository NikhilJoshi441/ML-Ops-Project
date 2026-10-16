# Runbook

The two most likely failure modes, ranked by what we saw in development and load tests.

---

## 1. Runs pile up: p95 run latency breaches the SLO (60 s), queue depth growing

**Symptom**
- Grafana "Run latency p50/p95" above 60 s; "Queue depth" `q:triage` or `q:liability` rising.
- The web client stays on `queued` / `liability` for a long time.
- Locust `complete_run` p95 climbs while `POST /v1/disputes` stays fast (submissions are accepted, completions lag).

**Confirmation**
1. `docker compose exec redis redis-cli llen q:liability` (and `q:triage`): is it growing?
2. Grafana "Stage latency p95 by agent" and "Per-step latency p95": is the time in `agent-worker` `llm:plan` / `llm:draft`
   (model-bound) or in `tool:*` (retriever-bound)?
3. Grafana "Provider calls by outcome", and `curl localhost:8082/admin/breaker`: are provider calls slow or timing out?
   If the breaker is open, go to failure mode 2.
4. `rate(run_e2e_latency_seconds_count[1m])` against `rate(disputes_submitted_total[1m])`: completion rate below
   arrival rate means capacity, not a bug. One liability worker sustains about 0.15 runs/s with a 1.2 s model (load test).

**Recovery**
1. Add liability capacity: `docker compose up -d --scale agent-worker=<n>` (measured: 1 -> 3 workers took throughput
   from 0.13 to 0.41 runs/s). If `q:triage` is the one growing, scale `triage-agent` too.
2. If the model is slow rather than overloaded, lower `AGENT_MAX_STEPS` / `AGENT_MAX_TOOL_CALLS` temporarily.
   This means fewer planner turns, more escalations and faster runs.
3. Protect the queue: the API refuses with 503 above `MAX_QUEUE_DEPTH`, and rate limiting is per client
   (`RATE_LIMIT_PER_MINUTE`). Check `rate_limited_total` for a single noisy client.
4. Afterwards, check for incomplete runs: `escalations_total{reason="INCOMPLETE_RUN"}`. They need an analyst.

---

## 2. LLM provider unavailable or rate-limited: circuit breaker open, every run escalates as DEGRADED_MODE

**Symptom**
- Grafana "Circuit breaker" panel at 2 (open) for `primary`; `llm_requests_total{outcome=~"timeout|error"}` rising.
- Results show `degraded: true` and `escalation_reasons` contains `DEGRADED_MODE`. Escalation rate jumps to about 100%.
- Gateway logs: `"provider call failed"` with timeouts, HTTP 429 or 5xx.

**Confirmation**
1. `curl -s localhost:8082/admin/breaker`: `state`, `recent_failures`, `open_until`, and `chaos`. Check that chaos is `ok`, not left on from a demo.
2. `docker compose logs --tail=50 llm-gateway | grep provider`: 429 means the free-tier quota is exhausted; timeouts or
   connection errors mean the provider (or Ollama on the host) is down.
3. Call the provider directly from the gateway container with the configured base URL, to rule out a key or network problem.

**Recovery**
1. If chaos was left on: `curl -XPOST localhost:8082/admin/chaos -d '{"mode":"ok"}' -H 'content-type: application/json'`.
2. Quota (429): wait for the window, or set `LLM_FALLBACK_BASE_URL` / `LLM_FALLBACK_MODEL` / `LLM_FALLBACK_API_KEY` to a
   second provider and `docker compose up -d llm-gateway`. The breaker then routes to the fallback automatically.
3. Provider down: same as 2, or point `LLM_BASE_URL` at local Ollama.
4. After recovery the breaker half-opens by itself after `BREAKER_OPEN_S` (one probe call). To force it:
   `curl -XPOST localhost:8082/admin/breaker/reset`.
5. Degraded runs are escalations, never answers. Analysts can re-submit a case (new `client_reference`) to get a full
   recommendation once the provider is back.

---

## Other checks

| Check | Command |
|---|---|
| Reconstruct a run | `curl -s localhost:8080/v1/runs/<id>/trace \| jq .summary` and Jaeger, searching tag `run.id=<id>` |
| Index freshness | `docker compose run --rm ingest python -m triage.ingest.pipeline status`; Grafana "Index age" |
| Re-index after a corpus update | edit `corpus/manifest.yaml` / replace the file, run `docker compose run --rm ingest`, then `curl -XPOST localhost:8081/admin/reload` |
| Rejected requests | `curl -s localhost:8080/v1/quarantine?limit=20` |
| Redis lost | runs in flight are lost (AOF limits this to about 1 s of writes); clients re-submit with the same `client_reference` |
