"""Canonical item-source selection for linked receipt evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional


@dataclass(frozen=True)
class CanonicalItem:
    name: str
    quantity: Optional[Decimal] = None
    unit_cost: Optional[Decimal] = None
    line_total: Optional[Decimal] = None


@dataclass(frozen=True)
class ItemSourceCandidate:
    evidence_id: int
    source_type: str
    extraction_confidence: Optional[float]
    items: tuple[CanonicalItem, ...]


@dataclass(frozen=True)
class ItemSourceResolution:
    evidence_id: int
    source_type: str
    items: tuple[CanonicalItem, ...]
    reason: str


class CanonicalItemResolver:
    """Choose one source for canonical line items while preserving all evidence."""

    SOURCE_PRIORITY = {
        "costco_e_receipt": 500,
        "electronic": 450,
        "instacart": 425,
        "email": 350,
        "scanned": 200,
        "photo": 150,
        "other": 100,
    }

    MIN_RELIABLE_CONFIDENCE = 0.80

    def resolve(self, candidates: Iterable[ItemSourceCandidate]) -> ItemSourceResolution:
        populated = [candidate for candidate in candidates if candidate.items]
        if not populated:
            raise ValueError("at least one linked evidence source must contain items")

        ranked = sorted(populated, key=self._rank_key, reverse=True)
        chosen = ranked[0]
        confidence = chosen.extraction_confidence
        structured = self.SOURCE_PRIORITY.get(chosen.source_type, 0) >= 400
        if structured and (confidence is None or confidence >= self.MIN_RELIABLE_CONFIDENCE):
            reason = "preferred_structured_source"
        elif chosen.source_type == "scanned":
            reason = "scanned_fallback"
        else:
            reason = "best_available_source"
        return ItemSourceResolution(chosen.evidence_id, chosen.source_type, chosen.items, reason)

    def _rank_key(self, candidate: ItemSourceCandidate) -> tuple[int, int, float, int, int]:
        priority = self.SOURCE_PRIORITY.get(candidate.source_type, 0)
        confidence = candidate.extraction_confidence if candidate.extraction_confidence is not None else 0.0
        reliable = int(confidence >= self.MIN_RELIABLE_CONFIDENCE)
        # Reliable data quality wins first; then source structure, completeness,
        # confidence and deterministic evidence id tie-breaker.
        return (reliable, priority, confidence, len(candidate.items), -candidate.evidence_id)


class PostgresCanonicalItemStore:
    def __init__(self, connection):
        self.connection = connection

    def apply(self, expense_pk: int, resolution: ItemSourceResolution) -> None:
        with self.connection.cursor() as cur:
            cur.execute("DELETE FROM budget.expense_items WHERE expense_pk = %s", (expense_pk,))
            for item in resolution.items:
                cur.execute(
                    """
                    INSERT INTO budget.expense_items (
                        expense_pk, item_name, unit_qty, unit_cost, line_total
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (expense_pk, item.name, item.quantity, item.unit_cost, item.line_total),
                )
            cur.execute(
                """
                INSERT INTO budget.canonical_item_source_resolutions (
                    expense_pk, evidence_id, source_type, reason, item_count, resolved_at
                ) VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (expense_pk) DO UPDATE SET
                    evidence_id = EXCLUDED.evidence_id,
                    source_type = EXCLUDED.source_type,
                    reason = EXCLUDED.reason,
                    item_count = EXCLUDED.item_count,
                    resolved_at = NOW()
                """,
                (expense_pk, resolution.evidence_id, resolution.source_type, resolution.reason, len(resolution.items)),
            )
        self.connection.commit()
