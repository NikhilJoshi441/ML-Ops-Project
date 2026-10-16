# Architecture

## Deployment diagram (boundaries = containers)

```
                       ┌──────────────────────────── docker compose network ─────────────────────────────┐
  analyst / client     │                                                                                  │
  web client ──HTTP──► │ ┌───────────┐  LPUSH q:triage   ┌───────────┐   BLMOVE    ┌──────────────────┐  │
  (POST, poll, trace)  │ │   api     │ ───────────────► │   redis   │ ──────────► │  triage-agent xN │  │
                       │ │ FastAPI   │ ◄─ run state ──── │ queues    │ ◄─ brief ── │  LangGraph:      │  │
                       │ │ validate  │                   │ run state │  q:liability│  guard→assess→   │  │
                       │ │ quarantine│                   │ events    │ ──────────► │  delay→fault→    │  │
                       │ │ ratelimit │                   │ caches    │             │  handoff         │  │
                       │ └───────────┘                   │ breaker   │             └────────┬─────────┘  │
                       │       ▲                         └─────┬─────┘                      │ /v1/chat   │
                       │       │ result / callback             │ BLMOVE q:liability          ▼           │
                       │       │                        ┌──────▼───────────┐  /v1/chat ┌──────────────┐ │
                       │       └─────────────────────── │ agent-worker xN  │ ────────► │ llm-gateway  │ │
                       │                                │ LangGraph ReAct: │           │ timeout/retry│ │
                       │                                │ plan⇄act→coverage│           │ breaker(redis│ │
                       │                                │ →draft→verify→   │           │ cache, cost  │ │
                       │                                │ (replan)→finalize│           └──┬────────┬──┘ │
                       │                                └───────┬──────────┘   primary    │        │    │
                       │                     search / clause /  │ related / support   ┌────▼──┐ ┌───▼──┐ │
                       │                                ┌───────▼──────────┐          │ext LLM│ │stub  │ │
                       │  ┌──────────┐  build index     │ retriever        │          │(Ollama│ │(load │ │
                       │  │ ingest   │ ───────────────► │ LangChain+Chroma │          │/free  │ │test) │ │
                       │  │ (job)    │  volume          │ + BM25 + clause  │          │tier)  │ └──────┘ │
                       │  └──────────┘  index-data      │ graph, embeddings│          └───────┘          │
                       │                                └──────────────────┘                             │
                       │  prometheus ◄── scrape all (DNS SD per replica) ── grafana     jaeger ◄─ OTLP   │
                       │  cadvisor (per-container CPU/memory)                                             │
                       └──────────────────────────────────────────────────────────────────────────────────┘
```

## Why these boundaries

| Service | Scaling | Failure isolation | Independent release | Resource profile |
|---|---|---|---|---|
| api | request rate; stateless (state in Redis) | a crash loses no work (queue + run state are in Redis) | contract changes | small, I/O bound |
| triage-agent | submissions; short stage (1 LLM call) | if it dies, the job is requeued by the reaper | prompt `triage.assess` | small |
| agent-worker | concurrent runs; long stage (4-9 LLM calls). The scaled unit (`--scale agent-worker=3`) | a dead replica's job is requeued **from the persisted TriageBrief**, so triage is not repeated | planner/drafting prompts, verifier | small CPU, waits on the LLM |
| retriever | read QPS; usually 1 replica | index problems cannot take down the API; agents fail closed (tool error -> escalate) | re-indexing / embedding-model changes | ~1-2 GB RAM (model + index), CPU-bound |
| ingest | one-shot job | runs before the retriever (`service_completed_successfully`) | corpus updates | CPU burst |
| llm-gateway | 1-2 replicas; breaker and cache shared through Redis | **only** holder of provider credentials; isolates provider outages behind the breaker | provider / model switch by config | small |
| llm-stub | load tests | n/a | n/a | small |
| redis | 1 (AOF persistence) | single point of failure, accepted for the coursework scope (see runbook) | n/a | small |

## Agent topology (A2)

Two agents in separate processes with a typed hand-off (`triage-brief v1`, `contracts/triage_brief.v1.schema.json`) through a queue:

1. **Triage agent.** Owns everything decidable from the dispute itself. Input guardrails (injection,
   toxicity); classification and fact extraction (LLM, prompt `triage.assess`); reporting delay
   (`compute_working_days` tool); fault hypothesis (deterministic). It never sees the corpus.
2. **Liability agent.** Owns the regulation. A ReAct loop in which the model **chooses** among
   `search_corpus`, `get_clause`, `get_related_provisions` and `lookup_request_fields`. Then a coverage
   check (do other provisions modify what was found?), drafting with chunk-level citations,
   verification, re-planning with a different strategy, and finalisation with output guardrails.

What happens if one fails mid-run:

| Failure | Behaviour |
|---|---|
| triage-agent process dies | job stays in its processing list; the reaper (any worker) sees the missing heartbeat and requeues it (attempt + 1) |
| agent-worker dies mid-run | same; the job restarts from the persisted brief. After `JOB_MAX_ATTEMPTS` the run is closed `failed` with route `ESCALATE_TO_ANALYST` and reason `INCOMPLETE_RUN`, `completed=false` |
| LLM provider down | gateway retries with backoff, then opens the breaker; fallback provider if configured; otherwise 503 `circuit_open` -> triage uses keyword rules, liability agent researches deterministically and returns **DEGRADED_MODE escalation** (never a confident answer) |
| retriever down | tool errors are observations; coverage cannot be met -> verification fails -> escalation |
| a tool returns junk / model invents clause ids | tool error recorded; duplicate calls skipped; citations that do not resolve fail verification |

## Loop bounds (A1)

`AGENT_MAX_STEPS=8` planner turns, `AGENT_MAX_TOOL_CALLS=12`, `AGENT_WALL_CLOCK_S=120`,
`AGENT_MAX_VERIFICATION_ATTEMPTS=2`, LangGraph `recursion_limit=60`, per-call timeouts on every tool
(`TOOL_TIMEOUT_S`) and LLM call (`LLM_TIMEOUT_S`, `GATEWAY_CALL_TIMEOUT_S`).

## Verification (A3)

Before anything is returned, `verify_draft` checks the following:
1. Schema: route and band are allowed values.
2. Every citation resolves to a passage retrieved in this run.
3. Every claim is supported by the passages it cites: sentence-level cosine >= 0.55, or >= 0.45 with
   content-token recall >= 0.5. Every number in the claim must appear in the cited text; case amounts
   and delays are exempt.
4. The governing provision families for the case are cited.
5. Band and route agree with the liability table (`agents/rules.py`). If the table says the case must
   be escalated (contested fault, conflicting provisions, outside corpus), the draft must escalate.
6. No unauthorised determinations appear in the text.

On failure, the agent re-plans with the next strategy (`agentic` -> `expand` -> `targeted`), feeds the
failure reasons back to the drafting prompt, and drafts again. After the last attempt it escalates
with `VERIFICATION_FAILED`. In that case it returns only claims that passed, plus the verbatim
governing provisions it retrieved.

## Retrieval and chunking (L1)

See the report, section 4. In short:
- **Clause-aware chunks.** One chunk per citable clause. Each is prefixed with its heading path and the
  parent's lead-in sentence.
- **Tables kept whole.** The sentence after Table 2 that defines how working days are counted is attached to it.
- **Clause graph.** Cross-references, defined-term sources, and curated cross-document `delegates_to`
  / `implements` relations (e.g. RBI 7(ii) "beyond seven working days -> bank's Board policy" ->
  FED 4.b(iii)). The `get_related_provisions` tool exposes it.
- **Hybrid search.** Dense retrieval (Chroma, cosine, via LangChain) and BM25 are fused with
  reciprocal-rank fusion.
- **Incremental re-embedding.** Deterministic chunk ids plus content hashes mean only changed chunks are
  re-embedded. `--reembed --doc` re-embeds a single document. A new embedding model gets its own collection.
