#!/usr/bin/env python3
"""Extract payment provenance from receipt OCR.

The OCR layer identifies payment method and card last-four only. Ownership/payer
resolution belongs in the database-backed payment-card lookup module.
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
    (re.compile(r'^\s*CARD\s+(?:TYPE\s*[:;]?\s*)?MC\b',re.I), 'Mastercard'),
    (re.compile(r"\bmaster\s*card\b|\bmastercard\b", re.I), "Mastercard"),
    (re.compile(r"\bvisa\b", re.I), "Visa"),
    (re.compile(r"\bamex\b|american\s+express", re.I), "Amex"),
    (re.compile(r"\binterac\b|\bdebit\b", re.I), "Debit"),
)

MASKED_LAST4_PATTERNS = (
    re.compile(r"\b(?:visa|master\s*card|mastercard|amex)(?:\s+credit(?:\s+card)?)?\s*[*xX#-][*xX#\s-]*(\d{4})\b", re.I),
    re.compile(r"\bcard\s+number\s*[:;]\s*[*xXKk#«»*\s-]+?(\d{4})\b", re.I),
    re.compile(r"\b(?:account|acct)\s*(?:number|#)?\s*[:;#-]?\s*[*xXKk#«»*\s-]+?(\d{4})\b", re.I),
)

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


def card_last4_evidence(text: str) -> list[dict]:
    """Retain masked tails with bounded terminal context and source line numbers."""
    lines = text.splitlines()
    readings = []
    for index, line in enumerate(lines):
        for pattern in MASKED_LAST4_PATTERNS:
            match = pattern.search(line)
            if match:
                readings.append({'value': match.group(1), 'line_numbers': [index + 1]})
        # OCR may split a terminal block into brand, entry mode, amount and
        # number rows. Cross only recognized terminal rows, never arbitrary
        # product, loyalty or address text, and cap the search at six rows.
        numbered_mask = re.match(
            r'^\s*NO[.,:;]?\s+[*xX#-][*xX#\s-]{3,}?(\d{4})\b', line, re.I,
        )
        if numbered_mask:
            for prior in range(index - 1, max(-1, index - 7), -1):
                context = lines[prior].strip()
                if re.match(r'^CARD\s+(?:MC|VISA|MASTER\s*CARD|AMEX|DEBIT|INTERAC)\b', context, re.I):
                    readings.append({'value': numbered_mask[1],
                                     'line_numbers': list(range(prior + 1, index + 2))})
                    break
                if not re.fullmatch(r'(?:RF|CHIP|TAP|CONTACTLESS|[*xX#-]+|\$?\s*\d+\.\d{2}|RCPT\s+\d+)?', context, re.I):
                    break
        # A standalone mask is card evidence only directly after a printed
        # payment/card entry-mode label, never after a loyalty/member label.
        mask = re.fullmatch(r'\s*[*xX#-][*xX#\s-]{3,}(\d{4})\s*', line)
        if mask and index and re.fullmatch(
                r'\s*(?:chip|contactless|tap|card|account|visa|master\s*card|amex|debit)\s*[:#]?\s*',
                lines[index-1], re.I):
            readings.append({'value': mask[1], 'line_numbers': [index, index + 1]})
    return readings


def extract_card_last4(text: str) -> Optional[str]:
    values = {r['value'] for r in card_last4_evidence(text)}
    return next(iter(values)) if len(values) == 1 else None


def extract_payment_provenance(text: str) -> PaymentProvenance:
    priority_lines = [line for line in text.splitlines() if PAYMENT_EVIDENCE_RE.search(line)
                      and not re.search(r'\b(?:earn|learn|apply|rewards|points)\b',line,re.I)]
    terminal = {method for line in text.splitlines()
                if re.match(r'^\s*CARD\s+(?:TYPE\s*[:;]?\s*)?(?:MC|VISA|MASTER\s*CARD|AMEX|DEBIT|INTERAC)\b',line,re.I)
                and (method:=normalize_payment_method(line))}

    method: Optional[str] = next(iter(terminal)) if len(terminal)==1 else None
    for line in priority_lines if not terminal else ():
        candidate = normalize_payment_method(line)
        if candidate:
            method = candidate
            break

    # Filtering rows before extracting a card can falsely make unrelated rows
    # adjacent. Preserve the original payment-block boundaries.
    last4 = extract_card_last4(text)

    if method is None:
        for line in text.splitlines():
            if CASH_TENDER_RE.search(line):
                method = "Cash"
                break

    return PaymentProvenance(payment_method=method, card_last4=last4)
