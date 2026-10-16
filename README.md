# Unauthorised Transaction and Card Dispute Triage Service

Mini Project 02, UE24AM342AA5 (ML System Design and AgentOps). Domain: retail banking.

A multi-agent service. It accepts a card or UPI dispute report and validates it at the boundary.
It classifies the fraud pattern, computes the reporting delay in working days, and finds the
liability provisions that apply in the RBI circular, the bank's Board-approved policy and the RBI
Integrated Ombudsman Scheme. It then returns a **verified, cited liability recommendation**:
chargeback initiation, provisional credit, or decline with reason. If fault is contested,
provisions conflict, the case is outside the corpus, or the answer cannot be verified, it
**escalates to a human** instead.

**The system never issues a credit or closes a dispute. A disputes analyst decides.** All data is synthetic.

| | |
|---|---|
| Team | _(names)_ |
| Section | _(section)_ |
| Report | [docs/REPORT.md](docs/REPORT.md) |
| Architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Runbook / incident log | [docs/RUNBOOK.md](docs/RUNBOOK.md) · [docs/INCIDENT_LOG.md](docs/INCIDENT_LOG.md) |
| SLO (declared before testing) | [docs/SLO.md](docs/SLO.md) |
| Data contracts | [docs/DATA_CONTRACTS.md](docs/DATA_CONTRACTS.md) · [contracts/](contracts/) |
| Evaluation set | [eval/scenarios.yaml](eval/scenarios.yaml) (24 scenarios) · results in [eval/results/](eval/results/) |
| Demo script | [docs/DEMO.md](docs/DEMO.md) |

---

## 1. Run it (Docker, from a clean clone)

Requirements: Docker with Compose v2, about 6 GB free disk, about 4 GB RAM for the containers, and internet on
the first start (Python wheels and the embedding model download once into a volume).

```bash
git clone <repo-url> card-dispute-triage && cd card-dispute-triage
cp .env.example .env
# edit .env: set EMBEDDING_MODEL to a sentence-transformers model id
# (we used the small MiniLM-L6-v2 model; see the comment in .env.example)
docker compose up --build -d
docker compose ps          # wait until api, retriever, agent-worker are "healthy" (first start: a few minutes)
```

The first `up` runs the `ingest` job: it parses, chunks, embeds and indexes the corpus in `corpus/raw/`.
The retriever starts only after ingest completes. Nothing runs outside the containers.

| URL | What |
|---|---|
| http://localhost:8080 | Web client: submit a dispute, see the recommendation, citations and escalation path |
| http://localhost:8080/docs | OpenAPI documentation of the REST interface |
| http://localhost:3000 | Grafana, dashboard **Dispute triage / Card dispute triage** (anonymous viewer) |
| http://localhost:16686 | Jaeger traces (service `api`, `triage-agent`, `agent-worker`, ...) |
| http://localhost:9090 | Prometheus |
| http://localhost:8082/admin/breaker | LLM gateway circuit-breaker state |

### Choosing the model provider (no code change)

The `llm-gateway` is the only service that talks to a model. Set three variables in `.env`:

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | `LLM_API_KEY` |
|---|---|---|---|
| Built-in stub (no key, fixed answers) | `http://llm-stub:8090/v1` | `stub` | empty |
| Ollama on the host | `http://host.docker.internal:11434/v1` | the tag you pulled | empty |
| Hosted free tier (any OpenAI-compatible API) | the provider's base URL | the provider's model id | your key |

Then run `docker compose up -d llm-gateway` to restart only the gateway. Set a spending limit on any paid account first.

### Without Docker (development)

```bash
python -m venv .venv && . .venv/bin/activate
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-dev.txt
cp .env.example .env         # set EMBEDDING_MODEL and the LLM_* variables (host-local URLs instead of service names)
redis-server --daemonize yes
scripts/run_local.sh start   # ingest (first time), retriever, gateway, stub, api, 1 triage + 1 liability worker
scripts/run_local.sh stop
```

---

## 2. Use it

```bash
# submit (returns 202 with a run id immediately)
curl -s -XPOST localhost:8080/v1/disputes -H 'content-type: application/json' -H 'X-Client-ID: me' \
     -d @docs/examples/section_4_1.json
# poll until status is completed
curl -s localhost:8080/v1/runs/<run_id>
# full reconstruction: every tool call, query, passage, prompt version, per-step latency and cost, verification
curl -s localhost:8080/v1/runs/<run_id>/trace
# analyst feedback
curl -s -XPOST localhost:8080/v1/runs/<run_id>/feedback -H 'content-type: application/json' \
     -d '{"satisfactory": true, "reason": "correct escalation"}'
```

Endpoints: `POST /v1/disputes`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/trace`, `POST /v1/runs/{id}/feedback`,
`GET /v1/feedback/summary`, `GET /v1/quality`, `GET /v1/quarantine`, `GET /v1/contracts`, `/healthz`, `/readyz`, `/metrics`.

Request contract: [contracts/dispute_report.v1.schema.json](contracts/dispute_report.v1.schema.json).
A malformed request returns **422** with field-level errors and a `quarantine_id`. No run is created.

---

## 3. Operate it

```bash
docker compose up -d --scale agent-worker=3                    # scale the liability agent
docker compose run --rm ingest python -m triage.ingest.pipeline build                     # incremental re-index
docker compose run --rm ingest python -m triage.ingest.pipeline build --reembed --doc FED-CPP-2025  # re-embed one document
docker compose run --rm ingest python -m triage.ingest.pipeline status                    # freshness record
curl -XPOST localhost:8082/admin/chaos -H 'content-type: application/json' -d '{"mode":"down"}'  # simulate provider outage
curl -XPOST localhost:8082/admin/chaos -H 'content-type: application/json' -d '{"mode":"ok"}'
```

Evaluation (needs `ALLOW_EXPERIMENT_OVERRIDES=true` in `.env`, then restart the api and agent-worker):

```bash
pip install httpx pyyaml        # host-side runner only
python eval/run_eval.py --label v2-clause --prompt-variant v2 --retrieval-profile clause
python eval/run_eval.py --compare v1-clause v2-clause
```

Load test (set `LLM_BASE_URL=http://llm-stub:8090/v1` first so the provider's rate limits are not measured):

```bash
LOCUST_USERS=16 LOCUST_DURATION=2m docker compose --profile loadtest up locust   # CSVs in loadtest/results/
```

Unit tests: `PYTHONPATH=src pytest -q tests`

---

## 4. Repository layout

```
src/triage/
  contracts/      pydantic data contracts (request, hand-off, result)
  api/            FastAPI edge: validation, quarantine, rate limit, submit/poll, trace, feedback, web client
  agents/         triage_agent.py, liability_agent.py (LangGraph), worker.py, tools, guardrails,
                  rules (liability table check), workdays, prompts/ (versioned YAML)
  retriever/      hybrid retrieval service (LangChain + Chroma + BM25), clause graph, support scoring
  ingest/         parse -> clause-aware chunking -> link -> embed -> index (idempotent CLI)
  llm_gateway/    provider interface: timeouts, retries, circuit breaker, fallback, cache, cost
  llm_stub/       OpenAI-compatible fixed-response model for load tests
  evaluation/     groundedness / faithfulness / context relevance / hallucination metrics
corpus/           manifest.yaml (+ provision families, cross-document relations) and raw documents
eval/             scenarios.yaml (24 scenarios), run_eval.py, results/
loadtest/         locustfile.py, cache_effect.py, results/
ops/              prometheus.yml, Grafana provisioning and dashboard
docs/             report, architecture, runbook, incident log, SLO, contracts, demo script, evidence
tests/            unit tests
```

## 5. Corpus

| Doc id | Document | Version | Source |
|---|---|---|---|
| RBI-CIRC-2017 | RBI circular DBR.No.Leg.BC.78/09.07.005/2017-18, *Customer Protection: Limiting Liability of Customers in Unauthorised Electronic Banking Transactions* | 6 Jul 2017 | rbi.org.in notification page (official HTML text layer) |
| FED-CPP-2025 | The Federal Bank Ltd, *Policy on Customer Protection: Limiting Liability of Customers in Unauthorised Electronic Banking Transactions* (Board-approved) | Sept 2025 | federal.bank.in (PDF, text layer) |
| RBI-IOS-2021 | *Reserve Bank - Integrated Ombudsman Scheme, 2021* (as amended 5 Aug 2022) | 12 Nov 2021 | full scheme text as republished by a regulated bank (PDF, text layer) |

The RBI's own PDF host (rbidocs) blocks scripted downloads. We therefore index the circular from the official
rbi.org.in HTML page, which has the same text. See [docs/INCIDENT_LOG.md](docs/INCIDENT_LOG.md), entry 1.
