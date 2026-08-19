#!/usr/bin/env python3
"""Extract payment provenance from receipt OCR.

The OCR layer identifies payment method and card last-four only. Ownership/payer
mapping belongs in configuration or canonical account data, not in OCR parsing.
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
    (re.compile(r"\bcash\b", re.I), "Cash"),
)

# Strong masked-card forms seen on Canadian receipt/payment terminals:
#   Visa *9809
#   VISA ************956
#   CARD NUMBER: ************0806
#   MASTERCARD S1053S0   (not safe enough; intentionally ignored)
MASKED_LAST4_PATTERNS = (
    re.compile(r"\b(?:visa|master\s*card|mastercard|amex)\s*[*xX#-]+\s*(\d{4})\b", re.I),
    re.compile(r"\bcard\s+number\s*:\s*[*xXKk#-]+\s*(\d{4})\b", re.I),
    re.compile(r"\b(?:account|acct)\s*(?:number|#)?\s*[:#-]?\s*[*xXKk#-]+\s*(\d{4})\b", re.I),
)


def normalize_payment_method(text: str) -> Optional[str]:
    for pattern, method in CARD_METHOD_PATTERNS:
        if pattern.search(text):
            return method
    return None


def extract_card_last4(text: str) -> Optional[str]:
    for line in text.splitlines():
        for pattern in MASKED_LAST4_PATTERNS:
            match = pattern.search(line)
            if match:
                return match.group(1)
    return None


def extract_payment_provenance(text: str) -> PaymentProvenance:
    method: Optional[str] = None
    last4: Optional[str] = None

    # Prefer payment-terminal/tender lines rather than merchant prose mentioning a
    # card brand. This also keeps Debit/Interac from being overwritten by Visa AID text.
    priority_lines = [
        line for line in text.splitlines()
        if re.search(
            r"\b(?:acct|card\s+type|card\s+number|tender|trans\s+type|visa|master\s*card|mastercard|interac|debit|amex|cash)\b",
            line,
            re.I,
        )
    ]
    for line in priority_lines:
        candidate = normalize_payment_method(line)
        if candidate:
            method = candidate
            break

    last4 = extract_card_last4("\n".join(priority_lines)) or extract_card_last4(text)
    return PaymentProvenance(payment_method=method, card_last4=last4)


def resolve_payer(last4: Optional[str], card_owner_map: dict[str, str]) -> Optional[str]:
    """Resolve payer from a caller-supplied last4 -> owner map.

    No guess is made when the card digits are absent or unknown.
    """
    if not last4:
        return None
    return card_owner_map.get(last4)
