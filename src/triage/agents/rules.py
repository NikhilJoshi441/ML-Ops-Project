"""Deterministic liability check.

The circular's core is a lookup table indexed by fault and reporting delay.
Arithmetic over that table is not delegated to a language model. The model
reads the complaint, researches the provisions and drafts the position; this
module is the independent check that the drafted band and route agree with
the table, and the source of the provision families the answer must cite.
It never returns an outcome on its own: its result is used by the verifier
and, in degraded mode, shown to the analyst as an unverified suggestion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from triage.config import get_settings
from triage.contracts.results import Category, EscalationReason, Facts, Fault, LiabilityBand, Route

CARD_CHANNELS = {"CARD_POS", "CARD_ATM", "CARD_ONLINE"}
REMOTE_SECRETS = {"OTP", "UPI_PIN"}

TABLE1_CAP = {
    "BSBD": 5000,
    "SAVINGS": 10000,
    "PREPAID_PPI": 10000,
    "CURRENT_MSME": 10000,
    "CURRENT_INDIVIDUAL_UPTO_25L": 10000,
    "CURRENT_OTHER": 25000,
}


@lru_cache
def provision_families() -> dict[str, dict]:
    p = Path(get_settings().corpus_dir) / "manifest.yaml"
    return yaml.safe_load(p.read_text()).get("provisions", {})


def table1_cap(account_type: str, credit_limit: float | None) -> int:
    if account_type == "CREDIT_CARD":
        return 10000 if (credit_limit or 0) <= 500000 else 25000
    return TABLE1_CAP.get(account_type, 10000)


@dataclass
class RuleOutcome:
    fault: Fault
    band: LiabilityBand
    route: Route
    customer_liability_inr: float | None
    required: list[str]                       # provision family keys that must be cited
    escalate: list[EscalationReason] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    alternative: dict | None = None           # the competing outcome when contested

    def as_dict(self) -> dict:
        return {
            "fault": self.fault.value, "band": self.band.value, "route": self.route.value,
            "customer_liability_inr": self.customer_liability_inr, "required": self.required,
            "escalate": [e.value for e in self.escalate], "notes": self.notes, "alternative": self.alternative,
        }


def attribute_fault(category: Category, facts: Facts, auth_method: str, channel: str) -> tuple[Fault, list[str]]:
    """Fault hypothesis from extracted facts. Returns (fault, contest_reasons)."""
    reasons: list[str] = []
    if category in (Category.MERCHANT_DISPUTE, Category.DUPLICATE_CHARGE):
        return Fault.NOT_UNAUTHORISED, reasons
    if category == Category.OUT_OF_SCOPE:
        return Fault.UNKNOWN, reasons
    shared = facts.credentials_shared == "YES"
    impersonated = facts.shared_with_impersonator == "YES"
    bank_alleged = facts.bank_system_failure_alleged == "YES"
    if shared and impersonated:
        reasons.append("Customer shared a credential with someone impersonating a trusted party: negligence under "
                       "the 'shared the payment credentials' example, or deception amounting to a third party breach. "
                       "The circular does not settle this.")
    if shared and bank_alleged:
        reasons.append("Customer shared a credential but also alleges a bank-side failure.")
    if not shared and auth_method in REMOTE_SECRETS:
        reasons.append(f"Transaction authenticated with {auth_method}: the bank's policy treats such transactions as "
                       "customer negligence, the customer does not admit sharing, and the burden of proof lies on the bank.")
    if category == Category.UPI_SOCIAL_ENGINEERING and not shared and facts.credentials_shared == "UNKNOWN":
        reasons.append("Social-engineering pattern without a clear account of what the customer disclosed or approved.")
    if reasons:
        return Fault.CONTESTED, reasons
    if bank_alleged:
        return Fault.BANK, reasons
    if shared:
        return Fault.CUSTOMER_NEGLIGENCE, reasons
    return Fault.THIRD_PARTY, reasons


def _third_party(wd: int, amount: float, cap: int) -> tuple[LiabilityBand, Route, float | None, list[str]]:
    if wd <= 3:
        return LiabilityBand.ZERO, Route.PROVISIONAL_CREDIT, 0.0, ["ZERO_THIRD_PARTY", "WORKING_DAYS", "SHADOW_REVERSAL"]
    if wd <= 7:
        return (LiabilityBand.LIMITED, Route.PROVISIONAL_CREDIT, float(min(amount, cap)),
                ["LIMITED_THIRD_PARTY", "LIABILITY_CAP_TABLE", "WORKING_DAYS"])
    # Beyond seven working days the circular defers to the bank's Board-approved policy;
    # the configured bank's policy (FED-CPP-2025 4.b(iii)) puts the entire value on the customer.
    return LiabilityBand.PER_BOARD_POLICY, Route.DECLINE_WITH_REASON, float(amount), ["BEYOND_SEVEN_DAYS", "WORKING_DAYS"]


def evaluate(category: Category, fault: Fault, working_days: int, amount: float, account_type: str,
             credit_limit: float | None, channel: str, out_of_corpus: bool = False) -> RuleOutcome:
    cap = table1_cap(account_type, credit_limit)
    if category == Category.OUT_OF_SCOPE or out_of_corpus:
        return RuleOutcome(Fault.UNKNOWN, LiabilityBand.UNDETERMINED, Route.ESCALATE_TO_ANALYST, None, [],
                           [EscalationReason.OUT_OF_CORPUS], ["The dispute is outside the indexed corpus."])
    if category == Category.DUPLICATE_CHARGE:
        return RuleOutcome(Fault.NOT_UNAUTHORISED, LiabilityBand.NOT_APPLICABLE, Route.CHARGEBACK_INITIATION, None,
                           ["ERRONEOUS_DEBIT"], notes=["Duplicate debit: an erroneous debit, not an unauthorised transaction."])
    if category == Category.MERCHANT_DISPUTE:
        if channel not in CARD_CHANNELS:
            return RuleOutcome(Fault.NOT_UNAUTHORISED, LiabilityBand.NOT_APPLICABLE, Route.ESCALATE_TO_ANALYST, None,
                               ["SCOPE_UNAUTHORISED"], [EscalationReason.OUT_OF_CORPUS],
                               ["Authorised non-card payment disputed with a merchant: no chargeback mechanism in the corpus."])
        return RuleOutcome(Fault.NOT_UNAUTHORISED, LiabilityBand.NOT_APPLICABLE, Route.CHARGEBACK_INITIATION, None,
                           ["SCOPE_UNAUTHORISED"], notes=["Customer authorised the payment; the unauthorised-transaction "
                                                          "liability framework does not apply."])
    if fault == Fault.BANK:
        return RuleOutcome(fault, LiabilityBand.ZERO, Route.PROVISIONAL_CREDIT, 0.0, ["ZERO_BANK_FAULT", "SHADOW_REVERSAL"])
    if fault == Fault.CUSTOMER_NEGLIGENCE:
        return RuleOutcome(fault, LiabilityBand.FULL_UNTIL_REPORTED, Route.DECLINE_WITH_REASON, float(amount), ["NEGLIGENCE"],
                           notes=["Loss after the report is borne by the bank."])
    if fault == Fault.THIRD_PARTY or fault == Fault.UNKNOWN:
        band, route, liab, req = _third_party(working_days, amount, cap)
        notes = [] if fault == Fault.THIRD_PARTY else ["No fault signals; treated as a third party breach because the "
                                                       "burden of proving customer liability lies on the bank."]
        if fault == Fault.UNKNOWN:
            req = req + ["BURDEN_OF_PROOF"]
        return RuleOutcome(Fault.THIRD_PARTY, band, route, liab, req, notes=notes)
    # CONTESTED: both readings are in play; a human must attribute fault.
    tp_band, tp_route, tp_liab, tp_req = _third_party(working_days, amount, cap)
    required = ["NEGLIGENCE"] + [r for r in tp_req if r not in ("SHADOW_REVERSAL",)] + ["BURDEN_OF_PROOF"]
    return RuleOutcome(
        Fault.CONTESTED, LiabilityBand.UNDETERMINED, Route.ESCALATE_TO_ANALYST, None, list(dict.fromkeys(required)),
        [EscalationReason.FAULT_CONTESTED, EscalationReason.PROVISIONS_CONFLICT],
        notes=[f"If negligence: customer bears the entire loss until reporting ({amount:,.0f} INR). "
               f"If third party breach reported after {working_days} working days: {tp_band.value} "
               f"({'n/a' if tp_liab is None else f'{tp_liab:,.0f} INR'})."],
        alternative={"negligence": {"band": "FULL_UNTIL_REPORTED", "route": "DECLINE_WITH_REASON", "liability_inr": amount},
                     "third_party": {"band": tp_band.value, "route": tp_route.value, "liability_inr": tp_liab}},
    )


def family_clauses(key: str) -> list[str]:
    return provision_families().get(key, {}).get("clauses", [])


def family_gap_text(key: str) -> str:
    return provision_families().get(key, {}).get("gap", key)


def covered(key: str, retrieved_ids: set[str]) -> list[str]:
    """Chunk ids in `retrieved_ids` that belong to provision family `key`."""
    hits = []
    for c in family_clauses(key):
        for rid in retrieved_ids:
            if rid == c or rid.split("~")[0] == c:
                hits.append(rid)
    return hits
