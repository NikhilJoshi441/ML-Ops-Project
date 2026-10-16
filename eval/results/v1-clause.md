# Evaluation: v1-clause

Config: `{"label": "v1-clause", "prompt_variant": "v1", "retrieval_profile": "clause", "started": "2026-10-16T05:55:46.147570+00:00", "wall_s": 945.9, "note": "aggregate excludes runs degraded by the circuit breaker during the batch (incident 6)"}`

## Aggregate

| metric | value |
|---|---|
| n | 14 |
| errors | 0 |
| route_accuracy | 0.5 |
| outcome_accuracy | 0.3571 |
| escalation_recall | 1.0 |
| escalation_precision | 0.4167 |
| gold_recall | 0.8333 |
| gold_cited | 0.8333 |
| groundedness | 1.0 |
| faithfulness | 1.0 |
| context_relevance | 0.3186 |
| hallucination_rate | 0.0 |
| draft_groundedness | 0.9286 |
| draft_faithfulness | 0.8571 |
| draft_hallucination_rate | 0.0714 |
| verification_pass_rate | 0.2143 |
| mean_tool_calls | 8.1429 |
| mean_llm_calls | 8.5714 |
| mean_prompt_tokens | 13922.5 |
| mean_completion_tokens | 1063.1429 |
| cost_per_100_runs_usd | 0.078118 |
| latency_p50_s | 182.5 |
| latency_p95_s | 268.32 |
| excluded_degraded_runs | 10 |

## Scenarios

| id | expected | got | band | outcome ok | verified | grounded | faithful | halluc. | latency s |
|---|---|---|---|---|---|---|---|---|---|
| S01_cnp_zero | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 268.32 |
| S02_skimming_zero | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 183.59 |
| S03_limited_savings | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 184.12 |
| S04_limited_bsbd | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S05_limited_credit_card_high_limit | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S06_beyond_seven_third_party | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S07_negligence_pin_shared | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S08_section_4_1 | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 2.01 |
| S09_upi_collect_fake_care | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 2.01 |
| S10_upi_screen_share | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 182.5 |
| S11_bank_fault | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 204.54 |
| S12_duplicate_charge | CHARGEBACK_INITIATION | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 172.46 |
| S13_merchant_not_delivered | CHARGEBACK_INITIATION | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 98.28 |
| S14_merchant_upi | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 216.7 |
| S15_out_of_scope_cheque | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 196.7 |
| S16_prompt_injection | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | ZERO | yes | True | 1.0 | 1.0 | 0 | 222.74 |
| S17_otp_denied | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | 1.0 | 1.0 | 0 | 116.32 |
| S18_boundary_three_days | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 230.76 |
| S19_boundary_four_days_ppi | PROVISIONAL_CREDIT | PROVISIONAL_CREDIT | LIMITED | yes | True | 1.0 | 1.0 | 0 | 172.59 |
| S20_boundary_eight_days | DECLINE_WITH_REASON | DECLINE_WITH_REASON | PER_BOARD_POLICY | yes | True | 1.0 | 1.0 | 0 | 154.44 |
| S21_no_alert_date | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 66.21 |
| S22_toxic_but_valid | PROVISIONAL_CREDIT | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 68.21 |
| S23_out_of_scope_insurance | ESCALATE_TO_ANALYST | ESCALATE_TO_ANALYST | UNDETERMINED | yes | False | None | None | 0 | 26.09 |
| S24_negligence_cvv_to_seller | DECLINE_WITH_REASON | ESCALATE_TO_ANALYST | UNDETERMINED | NO | False | 1.0 | 1.0 | 0 | 36.1 |
