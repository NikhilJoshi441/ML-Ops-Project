"""Generates ops/grafana/dashboards/dispute-triage.json (kept as code so panels stay reviewable)."""
import json
from pathlib import Path

panels, y = [], 0
pid = 0


def row(title):
    global y, pid
    pid += 1
    panels.append({"type": "row", "title": title, "id": pid, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False})
    y += 1


def ts(title, exprs, unit="short", w=8, h=7, x=None, kind="timeseries"):
    global pid
    pid += 1
    used = sum(p["gridPos"]["w"] for p in panels if p["gridPos"]["y"] == y and p["type"] != "row")
    p = {"type": kind, "title": title, "id": pid, "datasource": {"type": "prometheus", "uid": "prom"},
         "gridPos": {"x": used if x is None else x, "y": y, "w": w, "h": h},
         "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
         "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": l} for i, (e, l) in enumerate(exprs)]}
    panels.append(p)


def nl():
    global y
    y += 7


row("Service level (complete runs, submission -> final result)")
ts("Run latency p50 / p95 (SLO p95 <= 60 s)", [
    ('histogram_quantile(0.5, sum by (le) (rate(run_e2e_latency_seconds_bucket[5m])))', "p50"),
    ('histogram_quantile(0.95, sum by (le) (rate(run_e2e_latency_seconds_bucket[5m])))', "p95")], "s")
ts("Throughput (runs completed / s) and submissions", [
    ('sum(rate(run_e2e_latency_seconds_count[1m]))', "completed"),
    ('sum(rate(disputes_submitted_total[1m]))', "submitted"),
    ('sum(rate(disputes_rejected_total[1m]))', "rejected (quarantined)")], "reqps")
ts("Error rate", [
    ('sum(rate(run_e2e_latency_seconds_count{outcome="incomplete"}[5m])) / clamp_min(sum(rate(run_e2e_latency_seconds_count[5m])),1e-9)', "incomplete runs"),
    ('sum(rate(http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(http_requests_total[5m])),1e-9)', "HTTP 5xx")], "percentunit")
nl()
ts("Availability (targets up)", [('avg by (job) (up)', "{{job}}")], "percentunit", w=8)
ts("HTTP p95 latency by service", [('histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))', "{{service}}")], "s")
ts("Queue depth", [('max by (queue) (queue_depth)', "{{queue}}"), ('sum by (agent) (worker_in_flight)', "in flight {{agent}}")])
nl()
row("Agents")
ts("Task success rate by agent", [('sum by (agent) (rate(agent_runs_total{outcome="success"}[5m])) / clamp_min(sum by (agent) (rate(agent_runs_total[5m])),1e-9)', "{{agent}}")], "percentunit")
ts("Steps and tool calls per run (mean)", [
    ('sum by (agent) (rate(agent_steps_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(agent_steps_per_run_count[5m])),1e-9)', "steps {{agent}}"),
    ('sum by (agent) (rate(agent_tool_calls_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(agent_tool_calls_per_run_count[5m])),1e-9)', "tool calls {{agent}}"),
    ('sum by (agent) (rate(agent_llm_calls_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(agent_llm_calls_per_run_count[5m])),1e-9)', "llm calls {{agent}}")])
ts("Retry rate by agent (retries / calls)", [('sum by (agent) (rate(agent_retries_total[5m])) / clamp_min(sum by (agent) (rate(agent_calls_total[5m])),1e-9)', "{{agent}}")], "percentunit")
nl()
ts("Per-step latency p95", [('histogram_quantile(0.95, sum by (le, agent, step) (rate(agent_step_latency_seconds_bucket[5m])))', "{{agent}} {{step}}")], "s", w=12)
ts("Stage latency p95 by agent", [('histogram_quantile(0.95, sum by (le, agent) (rate(agent_latency_seconds_bucket[5m])))', "{{agent}}")], "s", w=6)
ts("Container uptime", [('time() - max by (agent, instance) (worker_start_time_seconds)', "{{agent}} {{instance}}"),
                        ('time() - max by (service) (service_start_time_seconds)', "{{service}}")], "s", w=6)
nl()
row("Resources per container")
ts("CPU (cores) per container", [('sum by (name) (rate(container_cpu_usage_seconds_total{name!=""}[1m]))', "{{name}}"),
                                 ('sum by (job, instance) (rate(process_cpu_seconds_total[1m]))', "{{job}} {{instance}} (process)")], "short", w=12)
ts("Memory per container", [('max by (name) (container_memory_working_set_bytes{name!=""})', "{{name}}"),
                            ('max by (job, instance) (process_resident_memory_bytes)', "{{job}} {{instance}} (process)")], "bytes", w=12)
nl()
row("LLM cost, cache, reliability")
ts("Cost USD/s by agent step", [('sum by (agent, step) (rate(llm_cost_usd_total[5m]))', "{{agent}} {{step}}")], "currencyUSD")
ts("Tokens/s by step", [('sum by (step, kind) (rate(llm_tokens_total[5m]))', "{{step}} {{kind}}")])
ts("Circuit breaker (0 closed, 1 half-open, 2 open)", [('max by (provider) (circuit_breaker_state)', "{{provider}}")])
nl()
ts("Cache hit ratio", [('sum(rate(llm_cache_total{result="hit"}[5m])) / clamp_min(sum(rate(llm_cache_total[5m])),1e-9)', "llm response cache"),
                       ('sum(rate(retrieval_cache_total{result="hit"}[5m])) / clamp_min(sum(rate(retrieval_cache_total[5m])),1e-9)', "retrieval cache")], "percentunit")
ts("Mean cost per run (USD)", [('sum(rate(run_cost_usd_sum[15m])) / clamp_min(sum(rate(run_cost_usd_count[15m])),1e-9)', "per run")], "currencyUSD")
ts("Provider calls by outcome", [('sum by (provider, outcome) (rate(llm_requests_total[5m]))', "{{provider}} {{outcome}}")], "reqps")
nl()
row("Quality, routing, guardrails")
ts("Online quality (sampled live runs)", [('max by (metric) (online_quality_last)', "{{metric}} (last)"),
     ('sum by (metric) (rate(online_quality_score_sum[30m])) / clamp_min(sum by (metric) (rate(online_quality_score_count[30m])),1e-9)', "{{metric}} (30m mean)")], "percentunit")
ts("Routes and escalation reasons", [('sum by (route) (increase(routes_total[1h]))', "{{route}}"), ('sum by (reason) (increase(escalations_total[1h]))', "esc: {{reason}}")])
ts("Guardrail triggers and analyst feedback", [('sum by (guard, signal) (increase(guardrail_events_total[1h]))', "{{guard}}:{{signal}}"),
                                               ('sum by (satisfactory) (increase(feedback_total[1h]))', "feedback satisfactory={{satisfactory}}")])
nl()
row("Index freshness")
ts("Index age", [('max by (profile) (index_age_seconds)', "{{profile}}")], "s")
ts("Chunks and ingest throughput", [('max by (profile) (index_chunks)', "chunks {{profile}}"), ('max by (profile, stage) (ingest_throughput_chunks_per_second)', "chunks/s {{profile}} {{stage}}")])
ts("Corpus versions indexed", [('max by (profile, doc_id, version) (index_document_info)', "{{profile}} {{doc_id}} {{version}}")], kind="table")

dash = {"title": "Card dispute triage", "uid": "dispute-triage", "schemaVersion": 39, "time": {"from": "now-1h", "to": "now"},
        "refresh": "10s", "panels": panels, "tags": ["mlops"]}
Path("ops/grafana/dashboards/dispute-triage.json").write_text(json.dumps(dash, indent=1))
print(len(panels), "panels")
