#!/usr/bin/env bash
# Run the whole stack as host processes (no Docker) - used for development and
# for the measurements in docs/REPORT.md when Docker is unavailable.
# Usage: scripts/run_local.sh start|stop   (reads .env if present)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python}
LOG=${LOG_DIR:-.local/logs}; mkdir -p "$LOG" .local/pids
[ -f .env ] && set -a && . ./.env && set +a
export PYTHONPATH=src REDIS_URL=${LOCAL_REDIS_URL:-redis://localhost:6379/0} RETRIEVER_URL=http://localhost:8081 \
       LLM_GATEWAY_URL=http://localhost:8082 CORPUS_DIR=corpus INDEX_DIR=${LOCAL_INDEX_DIR:-.local/index}
start() { # name cmd...
  local n=$1; shift
  nohup env SERVICE_NAME=$n "$@" > "$LOG/$n.log" 2>&1 & echo $! > .local/pids/$n.pid
}
case "${1:-start}" in
  start)
    redis-cli ping >/dev/null 2>&1 || (redis-server --port 6379 --save "" --daemonize yes)
    [ -f "$INDEX_DIR/index_meta.clause.json" ] || $PY -m triage.ingest.pipeline build
    start retriever  $PY -m uvicorn triage.retriever.service:app --port 8081
    start llm-stub   $PY -m uvicorn triage.llm_stub.service:app --port 8090
    start llm-gateway $PY -m uvicorn triage.llm_gateway.service:app --port 8082
    start api        $PY -m uvicorn triage.api.main:app --port 8080
    for i in $(seq 1 "${TRIAGE_REPLICAS:-1}"); do start triage-agent-$i env WORKER_METRICS_PORT=$((9110+i)) $PY -m triage.agents.worker triage; done
    for i in $(seq 1 "${WORKER_REPLICAS:-1}"); do start agent-worker-$i env WORKER_METRICS_PORT=$((9120+i)) $PY -m triage.agents.worker liability; done
    echo "started; logs in $LOG" ;;
  stop)
    for f in .local/pids/*.pid; do kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done; echo stopped ;;
esac
