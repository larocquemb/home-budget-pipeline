#!/usr/bin/env python3
"""Extract payment provenance from receipt OCR.

The OCR layer identifies payment method and card last-four only. Ownership/payer
resolution belongs in the database-backed payment_card_lookup module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass(frozen=True)
class PaymentProvenance:
    payment_method: Optional[str] = None
    card_last4: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


CARD_METHOD_PATTERNS = (
    (re.compile(r"\bmaster\s*card\b|\bmastercard\b", re.I), "Mastercard"),
    (re.compile(r"\bvisa\b", re.I), "Visa"),
    (re.compile(r"\bamex\b|american\s+express", re.I), "Amex"),
    (re.compile(r"\binterac\b|\bdebit\b", re.I), "Debit"),
)

# Strong masked-card forms seen on Canadian receipt/payment terminals:
#   Visa *9809
#   CARD NUMBER: KKKKKKKKKKKK0806 P
#   CARD NUMBER; xxxxxxxxxx** 3400
# OCR that does not preserve four numeric trailing digits is intentionally ignored.
MASKED_LAST4_PATTERNS = (
    re.compile(r"\b(?:visa|master\s*card|mastercard|amex)\s*[*xX#-]+\s*(\d{4})\b", re.I),
    re.compile(r"\bcard\s+number\s*[:;]\s*[*xXKk#«»*\s-]+?(\d{4})\b", re.I),
    re.compile(r"\b(?:account|acct)\s*(?:number|#)?\s*[:;#-]?\s*[*xXKk#«»*\s-]+?(\d{4})\b", re.I),
)

# Lines that strongly indicate the actual tender/payment instrument. Generic merchant
# prose such as "CASH SALE" must not outrank explicit card tender evidence.
PAYMENT_EVIDENCE_RE = re.compile(
    r"(?:\bacct\s*:|\bcard\s+type\s*:|\bcard\s+number\s*[:;]|\btender\b|"
    r"\btrans\s+type\s*:\s*purchase|\bvisa(?:\s+credit(?:\s+card)?)?\b|"
    r"\bmaster\s*card\b|\bmastercard\b|\binterac\b|\bdebit\b|\bamex\b)",
    re.I,
)

CASH_TENDER_RE = re.compile(r"^\s*(?:cash\s+(?:tender|payment)|tender\s+cash)\b", re.I)


def normalize_payment_method(text: str) -> Optional[str]:
    for pattern, method in CARD_METHOD_PATTERNS:
        if pattern.search(text):
            return method
    if CASH_TENDER_RE.search(text):
        return "Cash"
    return None


def extract_card_last4(text: str) -> Optional[str]:
    for line in text.splitlines():
        for pattern in MASKED_LAST4_PATTERNS:
            match = pattern.search(line)
            if match:
                return match.group(1)
    return None


def extract_payment_provenance(text: str) -> PaymentProvenance:
    # Prefer lines that describe an actual payment instrument. This prevents phrases
    # like "CASH SALE" or product text containing "cash" from masking card evidence.
    priority_lines = [line for line in text.splitlines() if PAYMENT_EVIDENCE_RE.search(line)]

    method: Optional[str] = None
    for line in priority_lines:
        candidate = normalize_payment_method(line)
        if candidate:
            method = candidate
            break

    last4 = extract_card_last4("\n".join(priority_lines)) or extract_card_last4(text)

    # Cash is only accepted from an explicit cash tender/payment line and only when
    # no stronger card/debit evidence was found.
    if method is None:
        for line in text.splitlines():
            if CASH_TENDER_RE.search(line):
                method = "Cash"
                break

    return PaymentProvenance(payment_method=method, card_last4=last4)
