.PHONY: up down scale3 logs ingest eval eval-all loadtest test demo-breaker dashboard
up:            ; cp -n .env.example .env 2>/dev/null || true; docker compose up -d --build
down:          ; docker compose down
scale3:        ; docker compose up -d --scale agent-worker=3
logs:          ; docker compose logs -f --tail=100 api triage-agent agent-worker
ingest:        ; docker compose run --rm ingest python -m triage.ingest.pipeline build
eval:          ; python eval/run_eval.py --label $${LABEL:-default}
eval-all:
	python eval/run_eval.py --label v1-clause --prompt-variant v1 --retrieval-profile clause
	python eval/run_eval.py --label v2-clause --prompt-variant v2 --retrieval-profile clause
	python eval/run_eval.py --label v2-naive  --prompt-variant v2 --retrieval-profile naive
loadtest:      ; docker compose --profile loadtest up locust
test:          ; PYTHONPATH=src pytest -q tests
demo-breaker:  ; curl -s -XPOST localhost:8082/admin/chaos -H 'content-type: application/json' -d '{"mode":"down"}'
dashboard:     ; python scripts/gen_dashboard.py
