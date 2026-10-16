# Data contracts (D1)

Every external interface has a versioned contract. The JSON Schemas in `contracts/` are generated from
the pydantic models (`python scripts/export_schemas.py`). They are also served live at `GET /v1/contracts`.

| Contract | Interface | Producer -> consumer | Schema |
|---|---|---|---|
| dispute-report v1.0 | `POST /v1/disputes` | client -> api | `contracts/dispute_report.v1.schema.json` |
| feedback v1 | `POST /v1/runs/{id}/feedback` | analyst -> api | `contracts/feedback.v1.schema.json` |
| triage-brief v1 | queue `q:liability` (run hash field `brief`) | triage-agent -> agent-worker | `contracts/triage_brief.v1.schema.json` |
| run-result v1 | `GET /v1/runs/{id}` `.result`, callback body | agent-worker -> client | `contracts/run_result.v1.schema.json` |
| retriever API | `/v1/search`, `/v1/clause/{id}`, `/v1/related`, `/v1/support`, `/v1/similarity`, `/v1/catalogue` | agents -> retriever | OpenAPI at `retriever:8081/docs` |
| llm-gateway API | `/v1/chat` | agents -> gateway | OpenAPI at `localhost:8082/docs` |

## dispute-report v1.0: validation at the ingestion boundary

All of these checks run in the API **before** a run is created. A failing request is written to the
`quarantine` Redis stream with its field-level errors (`GET /v1/quarantine`) and answered with 422 and a
`quarantine_id`. No run id is issued and nothing is enqueued.

| Layer | Rules |
|---|---|
| schema / type | JSON object; unknown fields rejected (`extra=forbid`); types for every field; datetimes ISO 8601 |
| range | `amount_inr` > 0 and <= 1,00,00,000; `complaint_text` 20-4000 chars (non-blank); `credit_limit_inr` > 0; `transaction_at` not in the future and not older than 3 years |
| format | `customer_id` `CUST-NNNNNN`; `transaction_id` `TXN-[A-Z0-9]{6,20}`; `card_last4` 4 digits; `client_reference` `[A-Za-z0-9_-]{4,64}`; `currency` `INR`; `callback_url` http(s) |
| categorical | `channel` {CARD_POS, CARD_ATM, CARD_ONLINE, UPI, NET_BANKING, MOBILE_BANKING}; `account_type` {BSBD, SAVINGS, PREPAID_PPI, CURRENT_MSME, CURRENT_INDIVIDUAL_UPTO_25L, CURRENT_OTHER, CREDIT_CARD} (the Table 1 rows); `auth_method`; `reporting_channel`; tri-state assertions {YES, NO, UNKNOWN} |
| cross-field | `reported_at` >= `transaction_at`; `alert_received_at` >= `transaction_at`; `card_last4` required for card channels and forbidden otherwise; `credit_limit_inr` required for CREDIT_CARD; CREDIT_CARD requires a card channel; UPI requires `auth_method` in {UPI_PIN, NONE, UNKNOWN}; `duplicate_of_transaction_id` != `transaction_id` |

Idempotency: the pair (`X-Client-ID`, `client_reference`) maps to one run. A resubmission returns the
existing run id with status `duplicate`.

The field `experiment` (prompt variant / retrieval profile) is for evaluation only. It is ignored unless
`ALLOW_EXPERIMENT_OVERRIDES=true`.

## run-result v1 (summary)

`route` is one of {CHARGEBACK_INITIATION, PROVISIONAL_CREDIT, DECLINE_WITH_REASON, ESCALATE_TO_ANALYST}.
`liability_band` is one of {ZERO, LIMITED, FULL_UNTIL_REPORTED, PER_BOARD_POLICY, NOT_APPLICABLE, UNDETERMINED}.
Each result also carries `customer_liability_inr`, `summary`, `basis`, `claims[{text, citations[chunk_id]}]`,
`citations[{chunk_id, clause_id, label, quote, source_url}]`, `escalation_reasons[]`, `escalation_path`,
`verification{passed, attempts, checks, failures}`, `degraded`, `completed`, `prompt_versions`, `cost_usd`, `tokens`.
`decision_authority` is always "Disputes analyst. This output is a recommendation only."
