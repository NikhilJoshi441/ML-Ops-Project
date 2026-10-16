# Demonstration script (brief, section 9)

All commands run from the repository root after `docker compose up -d --build`.
`jq` is optional but makes the output readable.

### 1. Clean clone to a working system
```bash
git clone <repo> && cd card-dispute-triage && cp .env.example .env   # set EMBEDDING_MODEL + LLM_* in .env
docker compose up --build -d && docker compose ps                    # all services healthy
curl -s localhost:8080/readyz && curl -s localhost:8081/readyz
```

### 2. Valid request -> routed recommendation with citations that resolve to retrieved text
```bash
RUN=$(curl -s -XPOST localhost:8080/v1/disputes -H 'content-type: application/json' -H 'X-Client-ID: demo' \
      -d @docs/examples/skimming_valid.json | jq -r .run_id)
watch -n2 "curl -s localhost:8080/v1/runs/$RUN | jq '.status, .result.route, .result.liability_band'"
curl -s localhost:8080/v1/runs/$RUN | jq '.result | {route, liability_band, claims, citations: [.citations[] | {chunk_id, label}]}'
```
Or use the web client at http://localhost:8080 with the sample "Skimming, reported next day".
Every `citations[].chunk_id` appears in the trace's `passages` (step 3).

### 3. One run traced end to end
```bash
curl -s localhost:8080/v1/runs/$RUN/trace | jq '.summary | {prompt_versions, queries, tool_calls, llm_calls, steps, cost_by_step, verification}'
```
Then in Jaeger (http://localhost:16686), service `api`, tag `run.id=<RUN>`. One trace spans
api -> triage-agent -> agent-worker -> retriever / llm-gateway, because trace context is carried through the queue.

### 4. Malformed request rejected and quarantined, no agent run
```bash
curl -s -XPOST localhost:8080/v1/disputes -H 'content-type: application/json' -d @docs/examples/malformed.json | jq
curl -s 'localhost:8080/v1/quarantine?limit=1' | jq '.items[0].errors'
docker compose exec redis redis-cli llen q:triage        # unchanged; no run id was issued
```
Recorded evidence: `docs/evidence/malformed_request_response.txt`, `docs/evidence/cross_field_rejection.txt`.

### 5. A case the corpus cannot answer -> escalation, not a fabricated answer
```bash
curl -s -XPOST localhost:8080/v1/disputes -H 'content-type: application/json' -d @docs/examples/out_of_corpus.json
# result: route ESCALATE_TO_ANALYST, escalation_reasons [OUT_OF_CORPUS]
```

### 6. Self-verification catching an unsupported answer, then a further search or escalation
Find a run whose trace has a failed first verification (eval scenarios S03/S07 usually show it), or submit
`docs/examples/section_4_1.json`:
```bash
curl -s localhost:8080/v1/runs/<id>/trace | jq '.events[] | select(.kind=="verification" or .kind=="retry") | {kind, attempt, passed, failures, strategy}'
```
The trace shows `verification passed=false` with reasons (e.g. "draft DECLINE_WITH_REASON but case requires
escalation", "citations do not resolve"), then a `retry` event with strategy `expand`, new `tool_call` events,
a second draft, and finally either a passed verification or `VERIFICATION_FAILED` escalation.
Recorded example: `docs/evidence/verification_retry_trace.json`.

### 7. Prompt injection and the guardrail response
```bash
curl -s -XPOST localhost:8080/v1/disputes -H 'content-type: application/json' -d @docs/examples/prompt_injection.json
# trace: guardrail event injection_score >= 0.5, signals [instruction_override, role_marker, output_steering, decision_steering],
# complaint withheld from every model; result ESCALATE_TO_ANALYST with GUARDRAIL_TRIGGERED
```

### 8. Circuit breaker opening when the provider is unavailable, and the degradation path
```bash
curl -s -XPOST localhost:8082/admin/chaos -H 'content-type: application/json' -d '{"mode":"down"}' | jq .primary
for i in 1 2 3; do curl -s -XPOST localhost:8080/v1/disputes -H 'content-type: application/json' \
   -d "$(jq ".client_reference=\"cb-$i-$RANDOM\"" docs/examples/skimming_valid.json)"; done
curl -s localhost:8082/admin/breaker | jq .primary       # state: open after 5 failures in 60 s
# results: degraded=true, route ESCALATE_TO_ANALYST, reasons [DEGRADED_MODE], basis lists the provisions retrieved deterministically
curl -s -XPOST localhost:8082/admin/chaos -H 'content-type: application/json' -d '{"mode":"ok"}'
# after BREAKER_OPEN_S the breaker half-opens, one probe succeeds, state returns to closed
```
Recorded evidence: `docs/evidence/circuit_breaker_demo.txt`.

### 9. Locust results, the failing concurrency, and the effect of --scale
```bash
# .env: LLM_BASE_URL=http://llm-stub:8090/v1, LLM_MODEL=stub
docker compose up -d llm-gateway
LOCUST_USERS=8 LOCUST_DURATION=1m docker compose --profile loadtest up locust
docker compose up -d --scale agent-worker=3
LOCUST_USERS=8 LOCUST_DURATION=1m docker compose --profile loadtest up locust
```
Results and analysis: `loadtest/results/SUMMARY.md` and the report, section 7.

### 10. The section 4.1 case
```bash
curl -s -XPOST localhost:8080/v1/disputes -H 'content-type: application/json' -d @docs/examples/section_4_1.json
```
Expected: `ESCALATE_TO_ANALYST`, reasons `FAULT_CONTESTED` and `PROVISIONS_CONFLICT`, and citations to **both**
RBI 7(i) / Federal Bank 4.b(i) (negligence: customer bears the loss until reporting) **and** RBI Table 2 /
Federal Bank 4.b(iii) (third party breach reported beyond seven working days -> bank's Board policy). The
summary states both competing outcomes. Recorded run: `docs/evidence/section_4_1_result.json`.
