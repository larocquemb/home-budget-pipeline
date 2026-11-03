"""Read CIBC's five-column, headerless card export without changing its rows."""
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import re


@dataclass(frozen=True)
class CibcRow:
    number: int
    date: date
    description: str
    amount: Decimal  # Purchases positive, credits negative; CAD by default.
    card_last4: str
    kind: str
    raw: list[str]
    fingerprint: str


def parse_cibc(data: bytes, account_reference: str) -> list[CibcRow]:
    if not account_reference.strip():
        raise ValueError('An account reference is required')
    occurrences = Counter()
    result = []
    for number, raw in enumerate(csv.reader(io.StringIO(data.decode('utf-8-sig')), strict=True), 1):
        if len(raw) != 5:
            raise ValueError(f'Row {number}: expected date, description, debit, credit, masked card')
        day, description, debit, credit, card = (v.strip() for v in raw)
        try:
            parsed_date = date.fromisoformat(day)
            if not description or bool(debit) == bool(credit):
                raise ValueError()
            if not re.fullmatch(r'\d+\.\d{2}', debit or credit):
                raise ValueError()
            amount = Decimal(debit or credit)
            if not amount.is_finite() or not 0 < amount < Decimal('1000000000000'):
                raise ValueError()
            if not re.fullmatch(r'\d{4}\*+\d{4}', card):
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError(f'Row {number}: invalid date, description, amount or masked card') from None
        amount *= 1 if debit else -1
        kind = 'purchase' if debit else ('payment' if re.search(r'\bPAYMENT\b|\bPAIEMENT\b', description, re.I) else 'credit')
        identity = [account_reference.strip().casefold(), day, ' '.join(description.casefold().split()),
                    str(amount), card[-4:]]
        key = json.dumps(identity, separators=(',', ':'))
        occurrences[key] += 1
        # Match the nth identical occurrence across overlapping exports, retaining
        # repeated purchases within an export. No filename or row-order identity.
        fingerprint = hashlib.sha256(json.dumps(['cibc-card-v1', *identity, occurrences[key]], separators=(',', ':')).encode()).hexdigest()
        result.append(CibcRow(number, parsed_date, description, amount, card[-4:], kind, raw, fingerprint))
    if not result:
        raise ValueError('The CSV is empty')
    return result
