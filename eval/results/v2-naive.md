# Evaluation: v2-naive

Config: `{"label": "v2-naive", "prompt_variant": "v2", "retrieval_profile": "naive", "started": "2026-10-16T05:55:46.147275+00:00", "wall_s": 859.0, "note": "aggregate excludes runs degraded by the circuit breaker during the batch (incident 6)"}`

## Aggregate

| metric | value |
|---|---|
| n | 9 |
| errors | 0 |
| route_accuracy | 0.5556 |
| outcome_accuracy | 0.5556 |
| escalation_recall | 1.0 |
| escalation_precision | 0.2 |
| gold_recall | 0.875 |
| gold_cited | 0.875 |
| groundedness | 1.0 |
| faithfulness | 1.0 |
| context_relevance | 0.3327 |
| hallucination_rate | 0.0 |
| draft_groundedness | 0.9444 |
| draft_faithfulness | 0.9444 |
| draft_hallucination_rate | 0.1111 |
| verification_pass_rate | 0.7778 |
| mean_tool_calls | 5.1111 |
| mean_llm_calls | 7.1111 |
| mean_prompt_tokens | 12028.2222 |
| mean_completion_tokens | 851.2222 |
| cost_per_100_runs_usd | 0.066951 |
| latency_p50_s | 160.46 |
| latency_p95_s | 220.69 |
| excluded_degraded_runs | 15 |

## Scenarios

| id | expected | got | band | outcome ok | verified | grounded | faithful | halluc. | latency s |
|---|---|---|---|---|---|---|---|---|---|
| S01_cnp_zero | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 422.75 |
| S02_skimming_zero | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | ZERO | yes | True | 1.0 | 1.0 | 0 | 123.93 |
| S03_limited_savings | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 183.58 |
| S04_limited_bsbd | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 58.18 |
| S05_limited_credit_card_high_limit | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S06_beyond_seven_third_party | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S07_negligence_pin_shared | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S08_section_4_1 | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 2.01 |
| S09_upi_collect_fake_care | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S10_upi_screen_share | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 2.01 |
| S11_bank_fault | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | True | 1.0 | 1.0 | 0 | 160.46 |
| S12_duplicate_charge | CHARGEBACK_INITIATION | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 184.49 |
| S13_merchant_not_delivered | CHARGEBACK_INITIATION | ESCALATE_TO_ANALYST | UNDETERMINED | NO | True | 1.0 | 1.0 | 0 | 160.5 |
| S14_merchant_upi | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 393.16 |
| S15_out_of_scope_cheque | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 50.2 |
| S16_prompt_injection | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 2.01 |
| S17_otp_denied | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S18_boundary_three_days | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S19_boundary_four_days_ppi | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S20_boundary_eight_days | DECLINE_WITH_REASON | DECLINE_WITH_REASON | PER_BOARD_POLICY | yes | True | 1.0 | 1.0 | 0 | 142.52 |
| S21_no_alert_date | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | ZERO | yes | True | 1.0 | 1.0 | 0 | 214.64 |
| S22_toxic_but_valid | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | ZERO | yes | True | 1.0 | 1.0 | 0 | 220.69 |
| S23_out_of_scope_insurance | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | True | 1.0 | 1.0 | 0 | 110.3 |
| S24_negligence_cvv_to_seller | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 90.4 |
