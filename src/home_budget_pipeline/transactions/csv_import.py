"""Institution-agnostic CSV transaction normalization for KAN-78."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from typing import Iterable, Mapping, Optional


@dataclass(frozen=True)
class FinancialTransaction:
    institution: str
    source_account_reference: str
    transaction_date: date
    posting_date: Optional[date]
    amount: Decimal
    description: str
    merchant_text: Optional[str]
    source_transaction_id: Optional[str]
    card_last4: Optional[str]
    currency: str
    raw_source: Mapping[str, str]
    import_fingerprint: str


def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unsupported date: {value}")


def transaction_fingerprint(
    *,
    institution: str,
    source_account_reference: str,
    transaction_date: date,
    posting_date: Optional[date],
    amount: Decimal,
    description: str,
    source_transaction_id: Optional[str] = None,
) -> str:
    """Stable dedupe key independent of filename/download overlap."""
    if source_transaction_id:
        identity = {
            "institution": institution.strip().lower(),
            "account": source_account_reference.strip().lower(),
            "source_transaction_id": source_transaction_id.strip(),
        }
    else:
        identity = {
            "institution": institution.strip().lower(),
            "account": source_account_reference.strip().lower(),
            "transaction_date": transaction_date.isoformat(),
            "posting_date": posting_date.isoformat() if posting_date else None,
            "amount": format(amount.quantize(Decimal("0.01")), "f"),
            "description": " ".join(description.lower().split()),
        }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class TransactionCsvNormalizer:
    """Normalize institution-specific CSV columns via an explicit field map."""

    def __init__(
        self,
        *,
        institution: str,
        source_account_reference: str,
        field_map: Mapping[str, str],
        amount_sign: Decimal = Decimal("1"),
        currency: str = "CAD",
    ) -> None:
        self.institution = institution
        self.source_account_reference = source_account_reference
        self.field_map = dict(field_map)
        self.amount_sign = amount_sign
        self.currency = currency

    def normalize_text(self, text: str) -> list[FinancialTransaction]:
        return list(self.normalize_rows(csv.DictReader(io.StringIO(text))))

    def normalize_rows(self, rows: Iterable[Mapping[str, str]]) -> Iterable[FinancialTransaction]:
        for raw in rows:
            transaction_date = _parse_date(raw.get(self.field_map["transaction_date"], ""))
            if transaction_date is None:
                raise ValueError("transaction_date is required")
            posting_col = self.field_map.get("posting_date")
            posting_date = _parse_date(raw.get(posting_col, "")) if posting_col else None
            description = raw.get(self.field_map["description"], "").strip()
            if not description:
                raise ValueError("description is required")
            amount = Decimal(raw.get(self.field_map["amount"], "0").replace(",", "").strip()) * self.amount_sign
            source_id_col = self.field_map.get("source_transaction_id")
            source_id = raw.get(source_id_col, "").strip() or None if source_id_col else None
            merchant_col = self.field_map.get("merchant_text")
            merchant = raw.get(merchant_col, "").strip() or None if merchant_col else None
            card_col = self.field_map.get("card_last4")
            card_last4 = raw.get(card_col, "").strip() or None if card_col else None
            fingerprint = transaction_fingerprint(
                institution=self.institution,
                source_account_reference=self.source_account_reference,
                transaction_date=transaction_date,
                posting_date=posting_date,
                amount=amount,
                description=description,
                source_transaction_id=source_id,
            )
            yield FinancialTransaction(
                institution=self.institution,
                source_account_reference=self.source_account_reference,
                transaction_date=transaction_date,
                posting_date=posting_date,
                amount=amount,
                description=description,
                merchant_text=merchant,
                source_transaction_id=source_id,
                card_last4=card_last4,
                currency=self.currency,
                raw_source=dict(raw),
                import_fingerprint=fingerprint,
            )
