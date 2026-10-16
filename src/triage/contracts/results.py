"""Contracts for the agent hand-off (triage-agent -> agent-worker) and for the
run result returned to callers (contract ids: triage-brief v1, run-result v1)."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Category(str, Enum):
    CARD_NOT_PRESENT_FRAUD = "CARD_NOT_PRESENT_FRAUD"
    SKIMMING = "SKIMMING"
    UPI_SOCIAL_ENGINEERING = "UPI_SOCIAL_ENGINEERING"
    MERCHANT_DISPUTE = "MERCHANT_DISPUTE"
    DUPLICATE_CHARGE = "DUPLICATE_CHARGE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class Fault(str, Enum):
    BANK = "BANK"
    THIRD_PARTY = "THIRD_PARTY"
    CUSTOMER_NEGLIGENCE = "CUSTOMER_NEGLIGENCE"
    CONTESTED = "CONTESTED"
    NOT_UNAUTHORISED = "NOT_UNAUTHORISED"
    UNKNOWN = "UNKNOWN"


class Route(str, Enum):
    CHARGEBACK_INITIATION = "CHARGEBACK_INITIATION"
    PROVISIONAL_CREDIT = "PROVISIONAL_CREDIT"
    DECLINE_WITH_REASON = "DECLINE_WITH_REASON"
    ESCALATE_TO_ANALYST = "ESCALATE_TO_ANALYST"


class LiabilityBand(str, Enum):
    ZERO = "ZERO"
    LIMITED = "LIMITED"
    FULL_UNTIL_REPORTED = "FULL_UNTIL_REPORTED"
    PER_BOARD_POLICY = "PER_BOARD_POLICY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNDETERMINED = "UNDETERMINED"


class EscalationReason(str, Enum):
    FAULT_CONTESTED = "FAULT_CONTESTED"
    PROVISIONS_CONFLICT = "PROVISIONS_CONFLICT"
    OUT_OF_CORPUS = "OUT_OF_CORPUS"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    DEGRADED_MODE = "DEGRADED_MODE"
    GUARDRAIL_TRIGGERED = "GUARDRAIL_TRIGGERED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INCOMPLETE_RUN = "INCOMPLETE_RUN"


class Facts(BaseModel):
    credentials_shared: str = "UNKNOWN"          # YES / NO / UNKNOWN
    shared_with_impersonator: str = "UNKNOWN"    # caller/message pretending to be bank, police, etc.
    card_in_possession: str = "UNKNOWN"
    alert_noticed: str = "UNKNOWN"
    bank_system_failure_alleged: str = "UNKNOWN"
    customer_denies_involvement: str = "UNKNOWN"
    authorised_by_customer: str = "UNKNOWN"      # customer made the payment themselves
    signals: list[str] = Field(default_factory=list)


class ReportingDelay(BaseModel):
    working_days: int
    calendar_days: int
    from_date: str
    to_date: str
    basis: str  # "alert_received_at" or "transaction_at (alert time not supplied)"
    excluded_holidays: list[str] = Field(default_factory=list)


class GuardrailReport(BaseModel):
    injection_score: float = 0.0
    injection_signals: list[str] = Field(default_factory=list)
    toxicity_score: float = 0.0
    toxicity_signals: list[str] = Field(default_factory=list)
    complaint_withheld_from_model: bool = False


class TriageBrief(BaseModel):
    """Hand-off message from the triage agent to the liability agent."""

    run_id: str
    category: Category
    category_confidence: float = 0.0
    category_rationale: str = ""
    facts: Facts
    fault_hypothesis: Fault
    contest_reasons: list[str] = Field(default_factory=list)
    reporting_delay: ReportingDelay
    guardrails: GuardrailReport
    sanitized_complaint: str
    degraded: bool = False
    prompt_versions: dict[str, str] = Field(default_factory=dict)


class Citation(BaseModel):
    chunk_id: str
    clause_id: str
    label: str
    quote: str
    source_url: str = ""


class Claim(BaseModel):
    text: str
    citations: list[str]
    supported: Optional[bool] = None
    support_score: Optional[float] = None


class VerificationReport(BaseModel):
    passed: bool
    attempts: int
    checks: dict[str, Any] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    """What the caller receives. Never an instruction to credit or close: an
    analyst decides (section 2 of the brief)."""

    run_id: str
    completed: bool
    route: Route
    liability_band: LiabilityBand
    customer_liability_inr: Optional[float] = None
    category: Optional[Category] = None
    fault_attribution: Optional[Fault] = None
    reporting_delay_working_days: Optional[int] = None
    summary: str
    basis: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    escalation_reasons: list[EscalationReason] = Field(default_factory=list)
    escalation_path: str = ""
    verification: Optional[VerificationReport] = None
    degraded: bool = False
    decision_authority: str = "Disputes analyst. This output is a recommendation only."
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: dict[str, int] = Field(default_factory=dict)
