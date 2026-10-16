"""Per-agent Prometheus metrics (A6, L5, L6). Exposed by each worker process on
WORKER_METRICS_PORT and scraped per replica."""
from prometheus_client import Counter, Gauge, Histogram

STEP_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)
RUN_BUCKETS = (1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300)

AGENT_RUNS = Counter("agent_runs_total", "Agent task outcomes", ["agent", "outcome"])
AGENT_STEPS = Histogram("agent_steps_per_run", "Graph steps per run", ["agent"], buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20))
AGENT_TOOLS = Histogram("agent_tool_calls_per_run", "Tool calls per run", ["agent"], buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 12))
AGENT_LLM_CALLS = Histogram("agent_llm_calls_per_run", "LLM calls per run", ["agent"], buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 12))
AGENT_STEP_LAT = Histogram("agent_step_latency_seconds", "Latency per agent step", ["agent", "step"], buckets=STEP_BUCKETS)
AGENT_LAT = Histogram("agent_latency_seconds", "Latency of an agent's whole stage", ["agent"], buckets=RUN_BUCKETS)
AGENT_RETRIES = Counter("agent_retries_total", "Retries (LLM gateway, tools, verification) by agent", ["agent", "kind"])
AGENT_CALLS = Counter("agent_calls_total", "LLM and tool calls by agent (denominator for retry rate)", ["agent", "kind"])
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls", ["agent", "tool", "outcome"])
RUN_E2E = Histogram("run_e2e_latency_seconds", "Submission to final result", ["outcome"], buckets=RUN_BUCKETS)
RUN_COST = Histogram("run_cost_usd", "LLM cost per run", buckets=(0, 1e-5, 5e-5, 1e-4, 2.5e-4, 5e-4, 1e-3, 2.5e-3, 5e-3, 1e-2))
RUN_TOKENS = Histogram("run_tokens", "Tokens per run", ["kind"], buckets=(0, 500, 1000, 2000, 4000, 8000, 16000, 32000))
STEP_COST = Counter("agent_step_cost_usd_total", "Cost attributed per agent step", ["agent", "step"])
VERIFY = Counter("verification_total", "Verification outcomes", ["outcome"])
ESCALATIONS = Counter("escalations_total", "Escalations by reason", ["reason"])
ROUTES = Counter("routes_total", "Final routes", ["route"])
GUARDRAIL = Counter("guardrail_events_total", "Guardrail triggers", ["guard", "signal"])
DEGRADED = Counter("degraded_runs_total", "Runs served by the deterministic fallback path", ["agent"])
QUALITY = Histogram("online_quality_score", "Automatic quality scores of sampled live runs", ["metric"],
                    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0))
QUALITY_LAST = Gauge("online_quality_last", "Latest sampled quality score", ["metric"])
WORKER_UP = Gauge("worker_start_time_seconds", "Worker process start time", ["agent"])
QUEUE_DEPTH = Gauge("queue_depth", "Jobs waiting", ["queue"])
IN_FLIGHT = Gauge("worker_in_flight", "Jobs in progress in this worker", ["agent"])
