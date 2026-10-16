# Load test summary (D6, A7)

**Target, declared in advance ([docs/SLO.md](../../docs/SLO.md)):** p95 of a complete run <= 60 s at a sustained
1 run/s, with fewer than 1% of runs incomplete.

**Setup.**
- Model replaced by `llm-stub`, which returns fixed responses after a log-normal delay (mean 1.2 s, sigma 0.35).
- Locust users each submit a dispute, poll every 1 s until the final result, then wait 1-3 s. The measured
  request `RUN complete_run` spans submission -> final result.
- One-minute steps, measured on the development host (Apple M-series, 11 cores, 18 GB). Services ran as host
  processes with the same topology as Compose (`scripts/run_local.sh`, `WORKER_REPLICAS=1|3`), because Docker was
  not available on the measuring machine.
- Raw CSVs: `w1_u*_stats.csv`, `w3_u*_stats.csv`.

| agent-worker replicas | users | submit rate (/s) | completed runs (/s) | complete-run p50 | complete-run p95 | failures | SLO p95 <= 60 s |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 0.15 | 0.14 | 12 s | 15 s | 0 | met (at 0.14 runs/s) |
| 1 | 8 | 0.25 | 0.12 | 34 s | 57 s | 0 | **failing**: queue grows without bound |
| 1 | 16 | 0.41 | 0.14 | 36 s | 56 s* | 0 | **failed** |
| 3 | 2 | 0.20 | 0.17 | 9 s | 10 s | 0 | met |
| 3 | 8 | 0.49 | 0.41 | 16 s | 22 s | 0 | met (at 0.41 runs/s) |
| 3 | 16 | 0.68 | 0.41 | 30 s | 39 s* | 0 | **failing**: arrivals 0.68/s > capacity 0.41/s, queue grows |

\* Survivorship: in a 1-minute window, runs still queued at the end are not counted. When arrivals exceed
capacity, the true p95 keeps growing with test length; the printed value is a lower bound.

**Where the target fails.**
- **1 worker:** p95 reaches the 60 s target at about 8 concurrent clients (0.25 submissions/s), where the queue starts to grow.
- **3 workers:** p95 holds at 8 clients but the queue grows at 16 clients (0.68 submissions/s).
- **Neither** configuration reaches 1 run/s, so the declared SLO is **not met** at the target load.

**Effect of `--scale agent-worker=3`.** Throughput rose from 0.13 to 0.41 runs/s (3.1x, linear). At 8 users, p95
fell from 57 s to 22 s. Scaling helps because the bottleneck is the worker pool itself.

**Actual bottleneck (worker metrics).**
- `agent-worker`: mean stage time 6.7 s per run, about 5.5 sequential LLM calls. Each replica processes one run
  at a time, so capacity is about 0.15 runs/s per replica (model-call latency x calls per run).
- `triage-agent`: 1.2 s per job, about 0.8 jobs/s per replica. This becomes the next bottleneck beyond about 5-6
  liability replicas.
- API submission p95 stayed under 60 ms throughout. Retriever tool calls are tens of milliseconds. Neither is a bottleneck.

**Estimated capacity to meet the SLO.** About 7-8 `agent-worker` replicas plus 2 `triage-agent` replicas reach
1 run/s with this model latency (an estimate from the per-replica rates above; not measured). Alternatives with
the same effect:
- async workers that run several jobs concurrently per process;
- fewer planner turns per run (each turn is a 1.2 s model call);
- a faster model.

## Caching (D7), `cache_effect.py`

The same dispute content was submitted 8 times in sequence (distinct client references, which models
resubmissions and re-keyed complaints), on the stub, with caches on vs off.

| | p50 | p95 | mean cost/run (USD) |
|---|---|---|---|
| caches off | 7.72 s | 9.52 s | 0.000572 |
| caches on (gateway response cache + retrieval cache) | 0.78 s | 10.52 s (= the cold first run) | 0.000156 |

Repeated content costs 73% less per run, and from the fourth repeat on, a run takes 0.26 s. p95 does not
improve at this sample size because it is the cold first run. Caching only helps repeated content. Workloads with
unique complaints see only the retrieval-cache and embedding-cache benefit, which is small: retrieval is a few ms.
Why a response cache is safe here: temperature 0, the key includes the full prompt and model, and the prompt
version is part of the prompt text, so a prompt change invalidates the cache.
