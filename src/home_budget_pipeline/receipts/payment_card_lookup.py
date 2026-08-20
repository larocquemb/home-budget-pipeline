"""Resolve receipt payment provenance against time-bounded card records."""

from __future__ import annotations

from datetime import date
from typing import Optional

from .payment import PaymentProvenance


def resolve_owner_from_db(
    conn,
    payment: PaymentProvenance,
    transaction_date: Optional[date | str],
    schema: str = "budget",
) -> Optional[str]:
    """Return a single matching owner, or None when unknown/ambiguous."""
    if not payment.card_last4 or not payment.payment_method or not transaction_date:
        return None

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT owner_name
            FROM {schema}.payment_cards
            WHERE lower(card_network) = lower(%s)
              AND card_last4 = %s
              AND %s::date >= valid_from
              AND (valid_to IS NULL OR %s::date <= valid_to)
            ORDER BY owner_name
            """,
            (
                payment.payment_method,
                payment.card_last4,
                transaction_date,
                transaction_date,
            ),
        )
        owners = [row[0] for row in cur.fetchall()]

    return owners[0] if len(owners) == 1 else None
