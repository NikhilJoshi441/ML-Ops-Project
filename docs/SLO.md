# Service level objective (declared before any load test)

Declared: 2026-10-16T05:24Z, before the first Locust run (D6). Not revised after seeing results.

**SLO: p95 of a complete run (submission -> final result available to the caller) <= 60 s,
at a sustained submission rate of 1 run/s, with fewer than 1% of runs incomplete.**

Measured from the moment the client sends POST /v1/disputes to the moment GET /v1/runs/{id}
first returns a final status, not the 202 of the submission call.

## Justification against the problem (brief section 1)

- The consumer is a disputes analyst working a queue. The recommendation must be ready by the
  time the analyst opens the case, and the web client is used interactively. Waiting more than
  about a minute breaks the workflow; a few seconds is not needed.
- The regulatory clocks are long (shadow credit within 10 working days, resolution within
  90 days), so latency is not driven by regulation. Correctness and the escalation behaviour are.
- Load: a large Indian bank receives in the order of thousands of unauthorised-transaction
  reports a day; even 20,000/day is about 0.25 runs/s on average. 1 run/s is about 4x that
  average and covers bursts such as a mass-skimming incident.
- One run makes about 6-9 model calls. With a realistic 1.2 s per call (stub delay) the
  model-bound floor is about 10-15 s, which leaves headroom for queueing within 60 s.
