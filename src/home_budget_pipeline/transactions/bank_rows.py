"""Normalized source rows shared by institution-specific CSV readers."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class BankRow:
    number: int
    date: date
    description: str
    amount: Decimal  # Outflows positive; inflows negative.
    card_last4: str | None
    kind: str
    raw: list[str]
    fingerprint: str
