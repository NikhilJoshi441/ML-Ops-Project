#!/usr/bin/env bash
# Steps the number of concurrent simulated clients and records Locust CSVs.
# Usage: loadtest/run_matrix.sh <tag> "<users list>" [duration]
set -euo pipefail
TAG=$1; USERS=${2:-"4 8 16"}; DUR=${3:-2m}; HOST=${HOST:-http://localhost:8080}
mkdir -p loadtest/results
for u in $USERS; do
  locust -f loadtest/locustfile.py --host "$HOST" --headless -u "$u" -r "$u" -t "$DUR" \
         --csv "loadtest/results/${TAG}_u${u}" --only-summary > "loadtest/results/${TAG}_u${u}.log" 2>&1 || true
  redis-cli --scan --pattern 'q:*' | xargs -r redis-cli del >/dev/null 2>&1 || true   # drain leftovers between steps
  sleep 5
done
