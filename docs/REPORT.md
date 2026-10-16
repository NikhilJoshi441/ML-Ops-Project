# Mini Project 02: Unauthorised Transaction and Card Dispute Triage Service

**Course:** UE24AM342AA5, ML System Design and AgentOps · **Component:** Jackfruit (Level 3)

| | |
|---|---|
| Team members | _______________________ |
| SRN | _______________________ |
| Section | _______________________ |
| Repository | _______________________ |

---

## 1. Problem and scope

Banks receive large volumes of unauthorised card and UPI transaction reports. For each one, an analyst does the following:

1. Reads the complaint.
2. Classifies the fraud pattern.
3. Counts the working days between the bank's alert and the customer's report.
4. Works out the customer's liability under the RBI circular of 6 July 2017. That liability is a table indexed by
   **who was at fault** and **how quickly the customer reported**.
5. Routes the case: chargeback, provisional (shadow) credit, or decline with a cited reason.

**What we built.** A production-style service that does the research half of this job and stops short of
the decision:
- It accepts a dispute report over a published REST contract and validates it at the boundary, quarantining rejects.
- It classifies the dispute and computes the delay.
- It finds the provisions that apply, including ones that modify them.
- It drafts a liability position with clause-level citations and verifies that draft against the retrieved text.
- It routes the case, or **escalates** when fault is contested, provisions conflict, the case is outside the
  corpus, or the answer cannot be verified.

It never credits or closes anything. Every result carries `decision_authority: Disputes analyst`.

**Corpus.**
- RBI circular DBR.No.Leg.BC.78/09.07.005/2017-18 (official HTML text).
- The Federal Bank's Board-approved customer protection policy (Sept 2025, PDF).
- The RBI Integrated Ombudsman Scheme 2021, as amended 2022 (PDF).

**Out of scope.** Real banking integrations (all data synthetic), model training, cloud deployment, card-network
chargeback rules (not in the corpus, so the system escalates when they would be needed, e.g. UPI merchant disputes).

**Prioritisation.** We built the end-to-end path first: contract -> queue -> agents -> cited result. Then
F5/F7/A3, which hold most of the difficulty. Then the reliability, observability and evaluation work.
Requirements we covered only partially are listed in section 9.

## 2. Agent design

### Topology (A2)

Two agents run in **separate containers** and hand off through a Redis queue, using a typed contract
(`triage-brief v1`).

| | Triage agent (`triage-agent`) | Liability agent (`agent-worker`) |
|---|---|---|
| Responsibility | everything decidable from the dispute itself | everything that needs the regulation |
| Graph (LangGraph) | guard_input -> assess -> compute_delay -> attribute_fault -> handoff | plan <-> act -> coverage -> draft -> verify -> (replan -> draft) -> finalize |
| LLM prompts | `triage.assess@v1` | `liability.plan@v1`, `liability.draft@v1/v2` |
| Tools | `lookup_request_fields`, `compute_working_days` | `search_corpus`, `get_clause`, `get_related_provisions`, `lookup_request_fields` |
| Sees the corpus | no | yes, only through tools |

**When an agent fails mid-run.**
- Each worker moves its job into its own processing list (`BLMOVE`) and heartbeats.
- A reaper requeues jobs held by dead workers. The liability stage restarts from the persisted brief, so triage is not repeated.
- After `JOB_MAX_ATTEMPTS` the run closes as `completed=false`, `ESCALATE_TO_ANALYST`, reason `INCOMPLETE_RUN`.
- An incomplete run is never shown as an answer.

### Retrieval is a tool, and the agent looks for modifiers (section 5 of the brief)

The planner (an LLM) chooses one tool per turn. It sees the case, a clause index, what it has retrieved so far,
and the **coverage gaps**. Coverage gaps are the provision families the case needs that have not been retrieved
yet: for example, "customer liability where reporting is delayed beyond seven working days". The method in the
prompt mirrors the brief:
1. Retrieve the provision that appears to apply.
2. Ask whether another provision modifies it, and call `get_related_provisions`. This tool walks the clause
   graph: cross-references, sources of defined terms, siblings and lead-ins, and curated cross-document
   relations such as RBI 7(ii) **delegates_to** Federal Bank 4.b(iii).
3. Search again with a query built from what was read.
4. Finish when no gaps remain.

A deterministic `coverage` node backs up steps 2-3. If gaps remain after the planner finishes, it re-plans once
with the gaps shown. If that fails, it runs targeted searches for the missing families, then queries derived from
cues in the text it has read ("Board approved policy", "Table 1", "working days"), then expands the graph.

**Why this matters for section 4.1.** A single embedding search for the 4.1 complaint returns the negligence
clauses but **not** the "beyond seven working days -> bank's Board policy" provision (verified: top-5 hybrid
results for the 4.1 query contain FED 4.b(i) but neither RBI 7(ii) nor Table 2). The clause graph reaches it in
one hop: RBI 7(i) -sibling-> 7(ii) -delegates_to-> FED 4.b(iii).

### Verification (A3)

Before returning, the verifier checks the following:
1. Schema: route and band are allowed values.
2. Every citation resolves to a passage retrieved **in this run**.
3. Every claim is supported by **its cited** passage. Support is a sentence-level embedding cosine >= 0.55, or
   >= 0.45 with content-token recall >= 0.5. Every number in a claim must also appear in the cited text, so an
   invented "Rs 50,000 cap" fails.
4. The governing provision families are cited.
5. Band and route agree with the liability table. A contested case **must** escalate.
6. No unauthorised determination ("has been credited", "dispute closed").

On failure, the agent re-plans with the next strategy (`agentic` -> `expand` -> `targeted`), sends the failure
reasons back to the drafting prompt, and drafts again. If the second attempt also fails, it escalates with
`VERIFICATION_FAILED`. It then returns only verified claims plus the governing provisions quoted verbatim.

### The liability table is checked deterministically (design decision)

The circular's core is a two-way lookup: fault by working days, with Table 1 caps by account type.
`agents/rules.py` encodes the outcome logic over **provision families** declared in `corpus/manifest.yaml`. Clause
ids are data, so swapping in another bank's policy is a manifest change.

The rule check is **not** the answer. The model reads the complaint, researches and drafts; the rules
independently check the band and route and say which families must be cited.

Fault attribution comes from facts the model extracts. The deterministic step only flags contradictions:
- credentials shared with an impersonator;
- OTP/UPI-PIN authentication while the customer denies sharing (the bank policy presumes negligence; RBI
  para 12 puts the burden of proof on the bank);
- credential sharing plus an alleged bank failure.

Each of these yields **CONTESTED**, which requires escalation with both readings cited. That is exactly the
behaviour section 4.1 asks for.

### Loop bounds (A1)

| Bound | Value |
|---|---|
| Planner turns | 8 per run (5 in the first phase, 3 per re-plan) |
| Tool calls | 12 |
| Wall-clock | 120 s |
| Verification attempts | 2 |
| LangGraph recursion limit | 60 |
| Per-call timeouts | every tool (10 s) and LLM call (30 s at the provider, 75 s at the gateway) |
| Duplicate tool calls | skipped; two duplicates end the planning phase |

## 3. Architecture and deployment boundaries

![architecture](ARCHITECTURE.md) (full diagram and table in `docs/ARCHITECTURE.md`)

```
client ─► api ─► redis q:triage ─► triage-agent(xN) ─► redis q:liability ─► agent-worker(xN) ─► result in redis ─► client polls / callback
                                         │                                     │      │
                                         └──────────► llm-gateway ◄────────────┘      └──► retriever (Chroma+BM25, model) ◄── ingest (job)
                                                       │  breaker/cache/cost (redis)
                                                       ├──► provider (Ollama / free tier)   └──► llm-stub (load tests)
prometheus (scrapes every replica) ─► grafana · OTLP traces ─► jaeger · cadvisor (per-container resources)
```

| Boundary | Reason |
|---|---|
| api | stateless edge; scales with request rate |
| triage-agent / agent-worker | different scaling: 1 vs 4-9 model calls per run; independently released prompts |
| retriever | memory/CPU profile of the embedding model and index; re-indexing releases |
| llm-gateway | sole holder of credentials; breaker and cache shared by all agents; provider switched by configuration |
| ingest | one-shot job ordered before the retriever |

There are 9 long-running services plus the ingest job and Locust. All start with one `docker compose up`, with
health checks and `depends_on` conditions.

**Async execution (A4).** `POST /v1/disputes` returns `202 {run_id}` at once. A Redis list is the queue between
submission and execution. The caller polls `GET /v1/runs/{id}`, or passes `callback_url`. We chose polling as the
default:
- a run takes tens of seconds;
- holding HTTP connections that long fails behind proxies with 30-60 s idle timeouts and loses work on disconnect;
- polling works through firewalls, and the callback is optional.

## 4. Chunking and embedding (L1)

**The difficulty (brief, section 3).** Table 2 is meaningless without "working days ... counted as per the working
schedule of the home branch excluding the date of receiving the communication". Item "(ii)" is meaningless without
its lead-in ("A customer shall be liable ... in the following cases"). And the "beyond seven working days" row
defers to a different document.

**Strategy.**
1. **Clause-aware segmentation.** We rebuild the paragraph -> lettered -> roman tree from numbering markers. HTML
   lists are converted to `(i)`, `(ii)`. Each document has its own chunk depth (circular: para + roman; bank
   policy: section + letter + roman; Ombudsman: clause + sub-clause). A chunk is a **citable clause** with a
   stable id such as `RBI-CIRC-2017:7(ii)` or `FED-CPP-2025:4.b(iii)`. The result is 119 chunks across 3 documents.
2. **Contextual embedding text.** We embed `[doc] [heading path] [label] (Lead-in: parent sentence) clause text`.
   Citations quote only the verbatim clause text.
3. **Tables whole.** A sentence after a table that names it is attached to the table chunk. This puts the
   working-day counting rule inside `RBI-CIRC-2017:Table-2`, enforced by a unit test.
4. **Clause graph.** We build three kinds of edge:
   - cross-references extracted from the text (`paragraph 7 (ii)`, `Table 1`, `clause 10`);
   - defined-term sources ("working days", "third party breach", "customer negligence", "Board approved policy",
     "shadow reversal", ...), each with the chunk that defines it;
   - six curated cross-document relations (`delegates_to`, `implements`, `escalation_path`).
5. **Hybrid search.** Dense retrieval (Chroma, cosine, orchestrated through LangChain `Chroma`/`HuggingFaceEmbeddings`)
   and BM25, fused with reciprocal-rank fusion. BM25 catches exact vocabulary such as "OTP", "Table 1" and "BSBD".

**Embedding model.** A small local sentence-transformers model (MiniLM-L6, 384 dimensions, normalised). It is free,
uses about 100 MB of RAM, and embeds the corpus in under 4 s.

**Idempotent, incremental indexing (D2).** `python -m triage.ingest.pipeline build`:
- Chunk ids are deterministic. Each chunk's content hash covers the embedding model and the text.
- Only changed chunks are embedded; removed chunks are deleted. A second build embedded **0 of 119** chunks.
- `--reembed --doc FED-CPP-2025` re-embeds one document only.
- A new embedding model writes a new collection, so the old one keeps serving.
- The freshness record (`index_meta.<profile>.json`) stores build time, document versions and sha256, chunk
  counts, and throughput (about 34 chunks/s embedding, 7.8 chunks/s end to end including model load). The
  retriever exposes all of it as Prometheus gauges.

The naive baseline profile (800-character windows, 100 overlap, no structure; 71 chunks) is built alongside it
for the before/after evaluation.

## 5. Design decisions and what was rejected

| Decision | Rejected alternative | Why |
|---|---|---|
| Two agents in separate processes with a queue hand-off | one process with two prompts | brief A2; independent scaling; a failure in one doesn't restart the other |
| LangGraph state machines | CrewAI | explicit, testable control flow (verify -> replan edges) and loop bounds |
| Retrieval as tools plus a clause graph | single-pass top-k RAG | single pass misses the modifying provision in 4.1 (section 2) |
| Deterministic table check plus LLM drafting | LLM decides the band | arithmetic over a regulatory table should not be sampled; gives the verifier an independent signal |
| Embedding-based support scoring for verification | LLM-as-judge | free, deterministic, fast; a 3B judge would be unreliable; the threshold was calibrated on paraphrase vs. wrong-number pairs |
| Gateway service owning breaker, cache and cost | client-side breaker per worker | a breaker shared through Redis trips once for all replicas; one place holds credentials |
| Redis lists with processing lists and a reaper | Celery / RQ | fewer moving parts; exactly the semantics we need (at-least-once, requeue on worker death) and visible in redis-cli |
| Poll by default, optional callback | synchronous HTTP, server-sent events | long runs; proxy timeouts; simplest client |
| Guardrails: regex/lexicon detectors plus structural defences | hosted moderation API, transformer toxicity model | no cost, no extra model in the hot path; structural defences (fencing, read-only tools, enum outputs, rule agreement) don't depend on detection |
| Chroma | FAISS | metadata filtering and incremental upsert/delete by id; persistence in a volume |
| Circular from the official HTML page | the rbidocs PDF | rbidocs serves a bot challenge to scripted clients (incident 1) |

## 6. Evaluation (L2, L3)

**Set.** `eval/scenarios.yaml` has 24 synthetic scenarios, each with an expected route/band/liability, expected
escalation reasons and the gold supporting clause(s). Dates are generated at run time to hit exact working-day
delays. The set covers:
- zero / limited / beyond-seven bands, including the 3/4/8-day boundaries;
- Table 1 caps for BSBD, savings, prepaid and high-limit credit cards;
- negligence, bank fault, contested OTP and UPI social engineering;
- duplicate and merchant disputes;
- two out-of-corpus cases, prompt injection, abusive language and a missing alert timestamp;
- **the section 4.1 case** (S08).

It runs as a batch through the public API: `python eval/run_eval.py --label ... --prompt-variant ... --retrieval-profile ...`.

**Model.** The evaluated system used a local qwen2.5 3B instruct model via Ollama. It is free, and was chosen over
the 7B model to keep turnaround short. Prices for cost accounting were set to a typical small hosted model
(USD 0.05 / 0.08 per 1M prompt/completion tokens).

**Metrics.** These are TruLens-equivalent, computed locally with the retriever's embedding model
(`src/triage/evaluation/metrics.py`). A claim counts as *supported* by a passage if sentence-level cosine >= 0.55
(or >= 0.45 with content-token recall >= 0.5) and all its numbers appear in the passage.

| Metric | How it is computed |
|---|---|
| Groundedness | share of the answer's claims supported by **any** passage retrieved in the run |
| Faithfulness | share of claims supported by the passages **they cite**, with every citation resolving to retrieved text |
| Context relevance | mean cosine between the case description and each distinct passage retrieved |
| Hallucination rate | share of runs with at least one claim unsupported by all retrieved passages, or a citation that does not resolve |
| Task metrics | route accuracy; outcome accuracy (route, band, liability and required escalation reasons all correct); escalation recall/precision; gold recall (gold clause retrieved); gold cited |

We report every metric twice:
- on the **final** answer (after verification);
- on the **first draft** (before verification), which shows what the verifier catches.

| metric | v1-clause | v2-clause | v2-naive (before) |
|---|---|---|---|
| n | 14 | 15 | 9 |
| excluded_degraded_runs | 10 | 9 | 15 |
| route_accuracy | 0.5 | 0.5333 | 0.5556 |
| outcome_accuracy | 0.3571 | 0.4667 | 0.5556 |
| escalation_recall | 1.0 | 1.0 | 1.0 |
| gold_recall | 0.8333 | 0.9286 | 0.875 |
| groundedness | 1.0 | 1.0 | 1.0 |
| faithfulness | 1.0 | 1.0 | 1.0 |
| context_relevance | 0.3186 | 0.3007 | 0.3327 |
| hallucination_rate | 0.0 | 0.0 | 0.0 |
| draft_groundedness | 0.9286 | 0.8667 | 0.9444 |
| draft_faithfulness | 0.8571 | 0.5333 | 0.9444 |
| draft_hallucination_rate | 0.0714 | 0.2 | 0.1111 |
| verification_pass_rate | 0.2143 | 0.4 | 0.7778 |
| mean_llm_calls | 8.5714 | 8.1333 | 7.1111 |
| mean_prompt_tokens | 13922.5 | 14781.0 | 12028.2222 |
| cost_per_100_runs_usd | 0.078118 | 0.081695 | 0.066951 |
| latency_p95_s | 268.32 | 202.83 | 220.69 |

Runs degraded by the circuit breaker during the batch (incident 6) are excluded from every aggregate; the counts are shown.
Per-scenario rows are in `eval/results/<label>.md`.

**Prompt variants (L2), v1 vs v2 on clause chunking.** v2 (an explicit decision procedure, a conflict rule and claim
discipline) performed better on the task metrics: route accuracy and outcome accuracy, plus verification pass rate.
v1's first drafts were more faithful, because it writes fewer, more literal claims. Final-answer groundedness and
faithfulness are 1.0 and the hallucination rate is 0 for **both** variants, because the verifier blocks unsupported
claims before return. The verifier's value is the gap between the draft and final rows.

**Deliberate change (L3): naive fixed-size chunking (before) -> clause-aware chunking with the clause graph (after),
both with prompt v2.** All four metrics are reported in the table: groundedness, faithfulness, context relevance and
hallucination rate, before and after. Clause chunking costs more per run (more tool calls and planner turns, because
the clause graph gives the agent more to follow). On the valid runs, naive chunking scored higher on outcome
accuracy (0.56 vs 0.47) and verification pass rate. But only 9 naive runs survived the provider outage, against 15
for clause, and they are a biased subset (the surviving runs were the shorter, simpler cases). Clause chunking had
better gold recall (0.93 vs 0.88). **We cannot claim the change improved the metrics.** The fair comparison is to
re-run the full set without the outage, using `make eval-all` with evaluation concurrency at 2.

**Section 4.1 (S08)** escalated correctly in every configuration, with FAULT_CONTESTED and PROVISIONS_CONFLICT, citing
both RBI 7(i) / FED 4.b(i) and RBI Table 2 / FED 4.b(iii). Escalation recall is 1.0 everywhere: the system never
answered a case that needed a human. Its weakness is over-escalation (low precision), because a 3B model often fails
verification on cases it should answer.


## 7. SLO, load test, bottleneck, cost

**SLO, declared before testing (`docs/SLO.md`).** p95 complete run (submission -> final result) <= 60 s at a
sustained 1 run/s, with fewer than 1% incomplete. The justification is the analyst workflow (a result ready when
the case is opened) and about 4x a large bank's average daily volume.

**Load test.** Locust, with the model replaced by `llm-stub` (fixed responses after a log-normal 1.2 s delay). It
measures submission -> final result. Full table: `loadtest/results/SUMMARY.md`.

| agent-worker replicas | users | completed runs/s | p95 complete run | met? |
|---|---|---|---|---|
| 1 | 2 | 0.14 | 15 s | yes, at 0.14 runs/s |
| 1 | 8 | 0.12 | 57 s (queue growing) | **fails here** |
| 3 | 8 | 0.41 | 22 s | yes, at 0.41 runs/s |
| 3 | 16 | 0.41 | 39 s (lower bound; queue growing) | **fails here** |

**Result.** The SLO is **not met** at the declared 1 run/s by either configuration.
- One worker sustains about 0.14 runs/s. The target fails at about 8 concurrent clients (0.25 submissions/s).
- `--scale agent-worker=3` gave a linear 3.1x throughput gain, to 0.41 runs/s, and cut p95 at 8 users from 57 s to 22 s.
- Three workers fail at 16 clients (0.68 submissions/s).

**Bottleneck.** The agent-worker pool. Each replica runs one job at a time, and a run is about 5.5 sequential
model calls (6.7 s with the stub). The API (p95 < 60 ms) and the retriever (tens of ms) are not bottlenecks.
Triage (1.2 s/job, about 0.8/s per replica) would become the next one beyond about 6 liability replicas. Meeting
1 run/s needs about 8 liability replicas, or concurrent jobs per worker, or fewer planner turns per run.

**Caching (D7).** On repeated content, caches cut cost per run from USD 0.000572 to 0.000156 (-73%) and the
median from 7.7 s to 0.8 s. Content in this setting repeats through resubmissions, re-keyed complaints and analyst
re-runs. p95 is unchanged at n=8 because it is the cold first run. Two more cost controls:
- **Rate limiting** per `X-Client-ID` (token bucket in Redis, default 30/min, burst 10). A looping client is cut
  off before it can multiply model calls.
- **Queue-depth back-pressure** (503 above 500 queued).

**Cost (L5).** Tokens and cost are recorded at call time, per step and per run (trace `cost_by_step`; Prometheus
`llm_cost_usd_total{agent,step}`). At the configured reference price (USD 0.05 / 0.08 per 1M tokens) the cost is
**USD 0.081695 per 100 runs** (v2-clause, about 14781.0 prompt tokens per run). The local
model itself is free. The **largest single cost driver is the planner step (`agent-worker:plan`)**: it runs several
times per run, and each call re-sends the case, the clause index and the retrieved-passage list. Prompt tokens are
about 94% of all tokens.

## 8. Guardrails and reliability (L4, A5)

**Prompt injection.**
- Weighted detectors for instruction override, role markers, chat-template tokens, delimiter breaking, decision
  and output steering, tool steering, prompt exfiltration and base64-encoded payloads.
- At or above 0.5, the complaint is **withheld from every model** and the run escalates with `GUARDRAIL_TRIGGERED`.
- Structural defences apply regardless of detection:
  - the complaint is fenced and declared untrusted in every prompt;
  - the model can only call read-only tools with allow-listed, validated arguments;
  - outputs are enums validated after generation;
  - the final route must agree with the rule check.

**Output filtering.** Sentences stating a credit, refund, reversal or closure, or claiming decision authority,
are replaced and logged (`guardrail_events_total{guard="output"}`).

**Toxicity.** Checked on input and output. Abusive input is masked, not rejected (S22 checks that an angry but
valid complaint is still served). Toxic output is blocked.

**Reliability (A5).**
- Timeouts on every tool and model call.
- Bounded retry with exponential backoff and jitter, at both the worker -> gateway and gateway -> provider hops.
- A Redis-backed circuit breaker per provider: 5 failures in 60 s opens it for 30 s, then a single half-open probe.
- An optional fallback provider.
- A final deterministic degraded path: keyword triage, rule-guided retrieval, and a `DEGRADED_MODE` escalation
  quoting the governing provisions, never a confident answer.
- `POST /admin/chaos {"mode":"down"|"slow"}` demonstrates all of this on request (`docs/evidence/circuit_breaker_demo.txt`).

**Observability (A6, D5).**
- JSON logs carry the correlation id (= run id) and trace id.
- OpenTelemetry spans for every step, tool and model call; trace context crosses the Redis queue inside the job message.
- Jaeger stores the traces. Prometheus scrapes every replica via DNS service discovery.
- The Grafana dashboard (32 panels) shows:
  - p50/p95, throughput, error rate, availability, and CPU and memory per container;
  - per-agent success rate, steps and tool calls per run, per-step latency, retry rate and uptime;
  - cost, tokens, cache hit ratios and breaker state;
  - online quality, routes and escalation reasons, guardrails and feedback;
  - index freshness.
- `GET /v1/runs/{id}/trace` reconstructs a run: every tool call with arguments and returned passages, queries,
  prompt versions with hashes, per-step latency, tokens and cost, and every verification outcome.

**Feedback and online quality (L6).**
- `POST /v1/runs/{id}/feedback` persists satisfactory/unsatisfactory plus a reason. The data is aggregated by
  route and prompt version at `GET /v1/feedback/summary`.
- A configurable share of live runs (`ONLINE_EVAL_SAMPLE_RATE`) is scored by the same metric code as the offline
  set, then charted in Grafana ("Online quality") and stored as a series at `GET /v1/quality`.

## 9. What did not work

- **A 3B local model is a weak planner.** It invented clause ids, repeated tool calls and copied enum lists from
  prompt templates (incident 4). After the fixes it still under-uses `get_related_provisions` on its own; the
  deterministic coverage strategy does much of the finding of modifying provisions. A larger hosted model would
  plan better. We kept the 3B model so every number in this report is reproducible at zero cost.
- **The system errs towards escalation.** Verification is strict (support threshold, every governing family
  cited). With a small model, many correct cases fail verification once or twice and end as `VERIFICATION_FAILED`
  escalations. This is safe but lowers outcome accuracy (section 6). Softening the thresholds would raise accuracy
  and the hallucination risk together, so we did not tune them on the evaluation set.
- **The SLO was not met** at 1 run/s (section 7). Workers process one job at a time; we measured the cause but did
  not implement concurrent workers.
- **Not verified under Docker on the measuring machine.** The development machine had no Docker. Every service
  was run and measured as a host process with the same topology (`scripts/run_local.sh`). The Compose file, images
  and health checks were written to the same commands but **must be checked with `docker compose up` before the
  demonstration**.
- **Working days ignore branch-specific holidays.** The calendar excludes Sundays, 2nd/4th Saturdays and three
  national holidays, plus anything in `EXTRA_HOLIDAYS`. The circular counts by the home branch's schedule, which the
  synthetic data does not carry. Every result states the basis.
- **Out-of-corpus detection relies on the triage category.** The retrieval-score threshold was useless with
  MiniLM: an unrelated loan/cheque query scored 0.76 cosine against the corpus.
- **Lexicon-based toxicity detection** has obvious recall limits (other languages, transliterated Hindi beyond the few terms listed).
