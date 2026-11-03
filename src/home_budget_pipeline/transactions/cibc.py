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
    card_last4: str | None
    kind: str
    raw: list[str]
    fingerprint: str


def parse_cibc(data: bytes, account_reference: str, *, account_type='credit_card', account_last4=None) -> list[CibcRow]:
    if not account_reference.strip():
        raise ValueError('An account reference is required')
    if re.search(r'\d{16}',account_reference):
        raise ValueError('Use an account label and last four digits, never a full account number')
    if account_type not in {'credit_card','chequing','savings'}:
        raise ValueError('Unsupported account type')
    deposit=account_type!='credit_card'
    if account_last4 is not None and not re.fullmatch(r'\d{4}',account_last4):
        raise ValueError('Account identifiers must contain exactly four digits')
    if deposit and not re.fullmatch(r'\d{4}',account_last4 or ''):
        raise ValueError('Deposit accounts require --account-last4 (exactly four digits)')
    occurrences = Counter()
    result = []
    for number, raw in enumerate(csv.reader(io.StringIO(data.decode('utf-8-sig')), strict=True), 1):
        if len(raw) != (4 if deposit else 5):
            raise ValueError(f'Row {number}: expected '+('four deposit-account columns' if deposit else 'date, description, debit, credit, masked card'))
        day, description, debit, credit = (v.strip() for v in raw[:4])
        card=raw[4].strip() if not deposit else None
        try:
            parsed_date = date.fromisoformat(day)
            if not description or bool(debit) == bool(credit):
                raise ValueError()
            if not re.fullmatch(r'\d+\.\d{2}', debit or credit):
                raise ValueError()
            amount = Decimal(debit or credit)
            if not amount.is_finite() or not 0 < amount < Decimal('1000000000000'):
                raise ValueError()
            if card is not None and not re.fullmatch(r'\d{4}\*+\d{4}', card):
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError(f'Row {number}: invalid date, description, amount or masked card') from None
        amount *= 1 if debit else -1
        kind = ('withdrawal' if debit else 'deposit') if deposit else ('purchase' if debit else ('payment' if re.search(r'\bPAYMENT\b|\bPAIEMENT\b', description, re.I) else 'credit'))
        identity = [account_reference.strip().casefold(), day, ' '.join(description.casefold().split()),
                    str(amount), account_last4 if deposit else card[-4:]]
        key = json.dumps(identity, separators=(',', ':'))
        occurrences[key] += 1
        # Match the nth identical occurrence across overlapping exports, retaining
        # repeated purchases within an export. No filename or row-order identity.
        format_name='cibc-deposit-v1' if deposit else 'cibc-card-v1'
        fingerprint = hashlib.sha256(json.dumps([format_name, *identity, occurrences[key]], separators=(',', ':')).encode()).hexdigest()
        result.append(CibcRow(number, parsed_date, description, amount, card[-4:] if card else None, kind, raw, fingerprint))
    if not result:
        raise ValueError('The CSV is empty')
    return result
