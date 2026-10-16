# Evaluation: v2-clause

Config: `{"label": "v2-clause", "prompt_variant": "v2", "retrieval_profile": "clause", "started": "2026-10-16T05:55:46.147480+00:00", "wall_s": 888.4, "note": "aggregate excludes runs degraded by the circuit breaker during the batch (incident 6)"}`

## Aggregate

| metric | value |
|---|---|
| n | 15 |
| errors | 0 |
| route_accuracy | 0.5333 |
| outcome_accuracy | 0.4667 |
| escalation_recall | 1.0 |
| escalation_precision | 0.3636 |
| gold_recall | 0.9286 |
| gold_cited | 0.8571 |
| groundedness | 1.0 |
| faithfulness | 1.0 |
| context_relevance | 0.3007 |
| hallucination_rate | 0.0 |
| draft_groundedness | 0.8667 |
| draft_faithfulness | 0.5333 |
| draft_hallucination_rate | 0.2 |
| verification_pass_rate | 0.4 |
| mean_tool_calls | 8.2 |
| mean_llm_calls | 8.1333 |
| mean_prompt_tokens | 14781.0 |
| mean_completion_tokens | 973.7333 |
| cost_per_100_runs_usd | 0.081695 |
| latency_p50_s | 165.54 |
| latency_p95_s | 202.83 |
| excluded_degraded_runs | 9 |

## Scenarios

| id | expected | got | band | outcome ok | verified | grounded | faithful | halluc. | latency s |
|---|---|---|---|---|---|---|---|---|---|
| S01_cnp_zero | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 165.54 |
| S02_skimming_zero | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 171.56 |
| S03_limited_savings | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 183.6 |
| S04_limited_bsbd | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | LIMITED | yes | True | 1.0 | 1.0 | 0 | 106.3 |
| S05_limited_credit_card_high_limit | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | LIMITED | yes | True | 1.0 | 1.0 | 0 | 136.4 |
| S06_beyond_seven_third_party | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S07_negligence_pin_shared | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S08_section_4_1 | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 2.01 |
| S09_upi_collect_fake_care | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | True | 1.0 | 1.0 | 0 | 200.55 |
| S10_upi_screen_share | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 172.45 |
| S11_bank_fault | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 140.4 |
| S12_duplicate_charge | CHARGEBACK_INITIATION | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 156.4 |
| S13_merchant_not_delivered | CHARGEBACK_INITIATION | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 28.09 |
| S14_merchant_upi | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 152.53 |
| S15_out_of_scope_cheque | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S16_prompt_injection | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 2.01 |
| S17_otp_denied | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S18_boundary_three_days | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S19_boundary_four_days_ppi | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | LIMITED | yes | True | 1.0 | 1.0 | 0 | 170.6 |
| S20_boundary_eight_days | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 180.58 |
| S21_no_alert_date | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 192.52 |
| S22_toxic_but_valid | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | ZERO | yes | True | 1.0 | 1.0 | 0 | 202.83 |
| S23_out_of_scope_insurance | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 106.44 |
| S24_negligence_cvv_to_seller | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | True | 1.0 | 1.0 | 0 | 94.45 |
