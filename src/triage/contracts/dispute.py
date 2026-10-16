"""Data contract for the dispute report accepted at the ingestion boundary (F1, F2, D1).

Contract id: dispute-report v1.0. The JSON Schema published in
contracts/dispute_report.v1.schema.json is generated from this model
(`python -m scripts.export_schemas`). Validation layers:

* schema/type  - pydantic types, extra fields forbidden
* range        - amounts, string lengths, dates within a plausible window
* categorical  - enums for channel, account type, auth method, ...
* cross-field  - date ordering, card fields required for card channels,
                 credit limit required for credit cards, etc.

A request that fails any layer is quarantined with field-level errors and no
agent run is created.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"
MAX_AMOUNT_INR = 10_000_000  # 1 crore: beyond this a human handles the case end to end
MAX_AGE_DAYS = 3 * 365


class Channel(str, Enum):
    CARD_POS = "CARD_POS"
    CARD_ATM = "CARD_ATM"
    CARD_ONLINE = "CARD_ONLINE"
    UPI = "UPI"
    NET_BANKING = "NET_BANKING"
    MOBILE_BANKING = "MOBILE_BANKING"


CARD_CHANNELS = {Channel.CARD_POS, Channel.CARD_ATM, Channel.CARD_ONLINE}


class AccountType(str, Enum):
    BSBD = "BSBD"
    SAVINGS = "SAVINGS"
    PREPAID_PPI = "PREPAID_PPI"
    CURRENT_MSME = "CURRENT_MSME"
    CURRENT_INDIVIDUAL_UPTO_25L = "CURRENT_INDIVIDUAL_UPTO_25L"
    CURRENT_OTHER = "CURRENT_OTHER"
    CREDIT_CARD = "CREDIT_CARD"


class AuthMethod(str, Enum):
    OTP = "OTP"
    PIN = "PIN"
    UPI_PIN = "UPI_PIN"
    CVV_ONLY = "CVV_ONLY"
    CONTACTLESS = "CONTACTLESS"
    MAGSTRIPE = "MAGSTRIPE"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class TriState(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class ReportingChannel(str, Enum):
    BRANCH = "BRANCH"
    PHONE = "PHONE"
    SMS_REPLY = "SMS_REPLY"
    EMAIL = "EMAIL"
    WEBSITE = "WEBSITE"
    MOBILE_APP = "MOBILE_APP"


class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=r"^CUST-\d{6}$", description="Synthetic customer id, CUST-NNNNNN")
    account_type: AccountType
    credit_limit_inr: Optional[float] = Field(default=None, gt=0, le=10_000_000)


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: str = Field(pattern=r"^TXN-[A-Z0-9]{6,20}$")
    channel: Channel
    amount_inr: float = Field(gt=0, le=MAX_AMOUNT_INR)
    currency: str = Field(default="INR", pattern=r"^INR$")
    transaction_at: datetime
    merchant_name: Optional[str] = Field(default=None, max_length=120)
    card_last4: Optional[str] = Field(default=None, pattern=r"^\d{4}$")
    auth_method: AuthMethod = AuthMethod.UNKNOWN
    duplicate_of_transaction_id: Optional[str] = Field(default=None, pattern=r"^TXN-[A-Z0-9]{6,20}$")


class Reporting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert_received_at: Optional[datetime] = Field(
        default=None, description="When the bank's SMS/e-mail communication about the transaction reached the customer"
    )
    reported_at: datetime
    reporting_channel: ReportingChannel


class CustomerAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_in_possession: TriState = TriState.UNKNOWN
    credentials_shared: TriState = TriState.UNKNOWN
    recognises_merchant: TriState = TriState.UNKNOWN


class Experiment(BaseModel):
    """Evaluation-only overrides. Ignored unless ALLOW_EXPERIMENT_OVERRIDES=true."""

    model_config = ConfigDict(extra="forbid")
    prompt_variant: Optional[str] = Field(default=None, pattern=r"^v\d+$")
    retrieval_profile: Optional[str] = Field(default=None, pattern=r"^(clause|naive)$")


class DisputeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^1\.\d+$")
    client_reference: str = Field(pattern=r"^[A-Za-z0-9_\-]{4,64}$", description="Caller's idempotency key")
    customer: Customer
    transaction: Transaction
    reporting: Reporting
    complaint_text: str = Field(min_length=20, max_length=4000)
    customer_assertions: CustomerAssertions = Field(default_factory=CustomerAssertions)
    callback_url: Optional[str] = Field(default=None, pattern=r"^https?://[^\s]{3,300}$")
    experiment: Optional[Experiment] = None

    @field_validator("complaint_text")
    @classmethod
    def _complaint_not_blank(cls, v: str) -> str:
        if len(v.strip()) < 20:
            raise ValueError("complaint_text must contain at least 20 non-whitespace characters")
        return v

    @model_validator(mode="after")
    def _cross_field(self) -> "DisputeReport":
        errors: list[str] = []
        now = datetime.now(timezone.utc)

        def aware(d: datetime) -> datetime:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

        txn_at = aware(self.transaction.transaction_at)
        rep_at = aware(self.reporting.reported_at)
        if txn_at > now + timedelta(minutes=5):
            errors.append("transaction.transaction_at: is in the future")
        if txn_at < now - timedelta(days=MAX_AGE_DAYS):
            errors.append(f"transaction.transaction_at: older than {MAX_AGE_DAYS} days")
        if rep_at > now + timedelta(minutes=5):
            errors.append("reporting.reported_at: is in the future")
        if rep_at < txn_at:
            errors.append("reporting.reported_at: must not precede transaction.transaction_at")
        if self.reporting.alert_received_at is not None:
            al = aware(self.reporting.alert_received_at)
            if al < txn_at:
                errors.append("reporting.alert_received_at: must not precede transaction.transaction_at")
            if al > rep_at + timedelta(days=1) and al > now:
                errors.append("reporting.alert_received_at: is in the future")
        if self.transaction.channel in CARD_CHANNELS and not self.transaction.card_last4:
            errors.append("transaction.card_last4: required for card channels")
        if self.transaction.channel not in CARD_CHANNELS and self.transaction.card_last4:
            errors.append("transaction.card_last4: must be absent for non-card channels")
        if self.customer.account_type == AccountType.CREDIT_CARD:
            if self.customer.credit_limit_inr is None:
                errors.append("customer.credit_limit_inr: required when account_type is CREDIT_CARD")
            if self.transaction.channel not in CARD_CHANNELS:
                errors.append("transaction.channel: a CREDIT_CARD account requires a card channel")
        if self.transaction.channel == Channel.UPI and self.transaction.auth_method not in (
            AuthMethod.UPI_PIN,
            AuthMethod.NONE,
            AuthMethod.UNKNOWN,
        ):
            errors.append("transaction.auth_method: UPI transactions use UPI_PIN, NONE or UNKNOWN")
        if self.transaction.duplicate_of_transaction_id == self.transaction.transaction_id:
            errors.append("transaction.duplicate_of_transaction_id: cannot reference itself")
        if errors:
            raise ValueError("; ".join(errors))
        return self


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    satisfactory: bool
    reason: str = Field(min_length=3, max_length=1000)
    analyst_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{2,40}$")
