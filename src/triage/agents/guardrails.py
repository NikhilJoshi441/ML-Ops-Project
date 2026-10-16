"""Guardrails (L4).

Input side (every free-text field that can reach a model):
* prompt-injection detection - weighted patterns for instruction override,
  role/system impersonation, tool or output steering, delimiter breaking and
  encoded payloads. Above the threshold the text is withheld from every model
  and the run is escalated; below it the text is still sanitised and fenced.
* toxicity detection - abusive / threatening language. Customers are often
  angry, so toxicity does not reject a dispute: abusive terms are masked
  before the model sees them and the run is flagged.

Output side:
* authorisation filter - the system may only recommend. Any sentence that
  states or implies that money was credited/refunded/reversed, that a dispute
  was closed/approved, or that claims decision authority is removed, and the
  event is recorded.
* toxicity check on generated text.

Structural defences that do not depend on detection: the complaint is fenced
between markers and declared untrusted in every prompt; the model only chooses
among read-only tools with validated arguments; routes and bands are enums
validated after generation; the final route must agree with the rule check.
"""
from __future__ import annotations

import base64
import re
import unicodedata

INJECTION_PATTERNS: list[tuple[str, float, str]] = [
    (r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|system|your)\b.{0,30}\b(instruction|prompt|rule|guideline|direction)s?", 0.8, "instruction_override"),
    (r"\b(you are now|act as|pretend to be|from now on you|new instructions?|developer mode|jailbreak|DAN)\b", 0.5, "role_reassignment"),
    (r"(^|\n)\s*(system|assistant|developer)\s*:", 0.5, "role_marker"),
    (r"<\s*/?\s*(system|assistant|instructions?|prompt)\s*>|\[/?INST\]|<\|im_(start|end)\|>", 0.6, "chat_template_tokens"),
    (r"COMPLAINT>>>|<<<COMPLAINT|PROMPT-ID", 0.7, "delimiter_breaking"),
    (r"\b(approve|authori[sz]e|issue|grant|process)\b.{0,30}\b(full\s+)?(refund|credit|chargeback|reversal)\b.{0,40}\b(immediately|now|without|automatically)", 0.5, "decision_steering"),
    (r"\b(output|respond|reply|return|print)\b.{0,20}\b(only|exactly)\b.{0,30}(json|\{|route|PROVISIONAL_CREDIT|ZERO)", 0.5, "output_steering"),
    (r"\b(call|use|invoke|run)\b.{0,20}\b(tool|function|get_clause|search_corpus|lookup_request_fields)\b", 0.4, "tool_steering"),
    (r"\b(reveal|show|print|repeat)\b.{0,30}\b(system prompt|instructions|hidden|api key|secret)", 0.6, "prompt_exfiltration"),
    (r"\b(liability|fault)\b.{0,30}\b(is|must be|should be)\b.{0,15}\b(zero|bank'?s? fault)\b.{0,30}\b(regardless|no matter|ignore)", 0.4, "conclusion_injection"),
]

TOXIC_TERMS = [
    "idiot", "idiots", "stupid", "moron", "morons", "bastard", "bastards", "scoundrel", "scoundrels", "useless fools",
    "bloody", "damn", "crap", "shit", "fuck", "fucking", "asshole", "bitch", "chor", "chors", "thieves", "liars",
]
THREAT_PATTERNS = [r"\b(kill|hurt|beat|attack|burn)\b.{0,20}\b(you|staff|manager|branch)\b", r"\b(bomb|shoot)\b"]

FORBIDDEN_OUTPUT = [
    (r"\b(has|have|had)\s+been\s+(credited|refunded|reversed|reimbursed|approved|closed|settled|resolved)\b", "states_completed_action"),
    (r"\b(we|i|the system)\s+(have|has|will)\s+(credit(ed)?|refund(ed)?|reverse(d)?|close(d)?|approve(d)?)\b", "claims_action"),
    (r"\b(dispute|complaint|case)\s+(is|stands)\s+(hereby\s+)?(closed|approved|resolved|rejected)\b", "closes_dispute"),
    (r"\b(i|we)\s+(hereby\s+)?(approve|authori[sz]e|decide|rule)\b", "claims_authority"),
    (r"\bfinal\s+(decision|determination)\b", "claims_finality"),
]


def _normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    return re.sub(r"[​-‏⁠﻿]", "", t)


def _decoded_payloads(text: str) -> list[str]:
    out = []
    for m in re.findall(r"[A-Za-z0-9+/=]{24,}", text):
        try:
            s = base64.b64decode(m + "=" * (-len(m) % 4)).decode("utf-8")
            if sum(c.isprintable() for c in s) / max(1, len(s)) > 0.9:
                out.append(s)
        except Exception:
            continue
    return out


def scan_injection(text: str) -> tuple[float, list[str]]:
    t = _normalise(text)
    candidates = [t] + _decoded_payloads(t)
    score, signals = 0.0, []
    for cand in candidates:
        for rx, w, name in INJECTION_PATTERNS:
            if re.search(rx, cand, re.I | re.S):
                if name not in signals:
                    signals.append(name)
                    score += w
    if len(candidates) > 1 and signals:
        signals.append("encoded_payload")
        score += 0.3
    return min(1.0, round(score, 3)), signals


def scan_toxicity(text: str) -> tuple[float, list[str]]:
    t = _normalise(text).lower()
    hits = [w for w in TOXIC_TERMS if re.search(rf"\b{re.escape(w)}\b", t)]
    threats = [p for p in THREAT_PATTERNS if re.search(p, t)]
    score = min(1.0, 0.25 * len(hits) + 0.6 * len(threats))
    return round(score, 3), hits + (["threat"] if threats else [])


def sanitise_for_model(text: str, max_len: int = 2500) -> str:
    t = _normalise(text)
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", t)
    t = t.replace("<<<", "«").replace(">>>", "»").replace("<|", "(").replace("|>", ")")
    t = re.sub(r"(?im)^\s*(system|assistant|developer)\s*:", r"[\1]:", t)
    for w in TOXIC_TERMS:
        t = re.sub(rf"\b{re.escape(w)}\b", "[redacted]", t, flags=re.I)
    return t[:max_len]


def filter_output(text: str) -> tuple[str, list[str]]:
    """Remove sentences that state a determination the system may not make."""
    violations: list[str] = []
    sentences = re.split(r"(?<=[.!?])\s+", text or "")
    kept = []
    for s in sentences:
        hit = next((name for rx, name in FORBIDDEN_OUTPUT if re.search(rx, s, re.I)), None)
        if hit:
            violations.append(hit)
            kept.append("[Removed: the system is not authorised to make this determination; an analyst decides.]")
        else:
            kept.append(s)
    tox, _ = scan_toxicity(text or "")
    if tox >= 0.5:
        violations.append("toxic_output")
    return " ".join(kept).strip(), violations
