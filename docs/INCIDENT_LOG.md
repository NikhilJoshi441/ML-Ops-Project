# Incident log

Real failures met during development, in the order they happened. Times are IST.

---

### 1. Corpus download returned HTML instead of the RBI PDF (2026-10-13)

- **What happened.** The scripted download of the RBI circular PDF from rbidocs.rbi.org.in, and of the Ombudsman
  Scheme from the same host, returned a 200 response with a JavaScript bot-challenge page. `file` reported HTML
  and `pypdf` failed with "invalid pdf header".
- **Impact.** The first index build would have indexed an HTML challenge page as "the circular".
- **Detection.** Manual inspection of `file *.pdf` output. No pipeline check existed yet.
- **Resolution.** We indexed the circular from the official rbi.org.in notification page (HTML, same text) and
  the full Ombudsman Scheme from a regulated bank's republished PDF. The source of each is recorded in
  `corpus/manifest.yaml`. The pipeline now records the sha256 of every source file in the freshness record, so a
  silently changed download shows up as a new version.
- **Follow-up.** Ingest should reject a "PDF" whose bytes do not start with `%PDF`. Not yet implemented.

### 2. The whole circular became one chunk (2026-10-13)

- **What happened.** The first segmentation run of the circular produced 23 "preamble" chunks and no clause ids.
  The RBI page lays out its content inside an HTML `<table>`, and the parser treated that layout table as a data
  table, flattening the entire circular into a single "row".
- **Detection.** The segmentation smoke test listed only `RBI-CIRC-2017:preamble~pN` ids.
- **Resolution.** A table that contains block elements (`table`, `ol`, `div`, or more than two `<p>`) is now
  walked as layout. Only leaf tables are rendered as rows.

### 3. Table 2 rows silently missing (2026-10-13)

- **What happened.** After fix 2, the `Table-2` chunk contained only the working-day sentence. The three rows
  ("within 3 / 4 to 7 / beyond 7 working days") were missing. The table's caption cell opens a `<p>` that is never
  closed, so the table matched the new "contains `<p>`" layout heuristic, and its rows were emitted as orphan lines.
- **Impact.** This is exactly the section 3 failure: the liability table separated from its working-day definition.
- **Resolution.** The layout heuristic now requires more than two `<p>` elements. A unit test
  (`test_clause_segmentation_keeps_table_with_working_day_rule`) asserts that Table 2 contains both the
  "Beyond 7 working days" row and the counting rule.

### 4. Every evaluation case escalated with the 3B model (2026-10-16)

- **What happened.** In the first smoke evaluation, 4 of 4 cases were routed to ESCALATE_TO_ANALYST, including a
  plain zero-liability case and a duplicate charge. The traces showed:
  - the planner inventing clause ids (`RBI-CIRC-2017:1(i) shared the payment credentials`);
  - the planner calling `get_related_provisions` on the same clause four times, which exhausted the tool budget
    before the provisions the case needed were retrieved;
  - drafts with band values like `"FULL_UNTIL_REPORTED|PER_BOARD_POLICY|..."` copied from the prompt template;
  - citations written as `"RBI-CIRC-2017:7(i) and FED-CPP-2025:4.b(i)"`;
  - a card transaction classified as `UPI_SOCIAL_ENGINEERING`, and a cousin classed as an "impersonator".
- **Impact.** The system failed safe (verification blocked every bad draft) but was useless (no recommendations).
- **Resolution.**
  - The planner prompt now receives a clause index and is told to copy ids exactly.
  - Duplicate tool calls are skipped and counted; two duplicates end the planning phase.
  - The deterministic gap-filling strategy runs targeted searches before graph expansion.
  - The drafting prompts list allowed values separately from the JSON template.
  - The citation parser splits "A and B" (without altering ids).
  - Category/channel contradictions are overridden from the validated channel field.
  - The triage prompt defines "impersonator" precisely.
- **Residual.** A 3B local model remains weak at multi-step planning. See the report, "What did not work".

### 5. Locust would not start (2026-10-16)

- **What happened.** `locust --version` failed with `ModuleNotFoundError: No module named 'zope.event'`. The
  development virtualenv inherited system site-packages that shipped a partial `zope` namespace package.
- **Resolution.** Locust runs from its own clean virtualenv locally, and from its own image
  (`loadtest/Dockerfile`) under Compose.

### 6. Circuit breaker opened during the evaluation batch; runs degraded (2026-10-16)

- **What happened.** We ran the three evaluation configurations in parallel (9 concurrent runs, 9 liability
  workers, 2 triage workers) against one local Ollama instance. Queued requests exceeded `LLM_TIMEOUT_S=60` at the
  provider. The gateway retried with backoff, recorded 5 failures inside 60 s, and **opened the breaker**.
  For the next 30 s every model call was refused immediately (`503 circuit_open`).
- **Impact.** 23 of 72 evaluation runs (6 / 7 / 10 per configuration) took the degraded path. Each finished in
  about 2 s as `ESCALATE_TO_ANALYST` with `DEGRADED_MODE`, quoting the governing provisions retrieved
  deterministically. None returned a confident answer, so the system behaved as designed. The evaluation numbers
  for those runs were invalid, though, because they measured the fallback, not the agent.
- **Detection.** Latency column in `eval/results/*.md`: a cluster of exactly 2.01 s runs. The run results carried
  `DEGRADED_MODE`. Grafana "Circuit breaker" would have shown state 2.
- **Resolution.** We raised `LLM_TIMEOUT_S` to 150 s for the local model and cut evaluation concurrency to 2 per
  configuration. Then we re-ran exactly the degraded scenarios and merged them back
  (`eval/run_eval.py --merge <label> <label>-rerun`). The ids that were re-run are listed in each results file
  under `meta.rerun_ids`.
- **Lesson.** Size the provider timeout to the provider's real queueing behaviour, and do not let an offline
  batch share a provider with itself at higher concurrency than the provider can serve. In production the
  evaluation batch should run against its own provider quota.
