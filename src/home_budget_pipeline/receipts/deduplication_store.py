"""Persistence for auditable receipt duplicate decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .deduplication import DuplicateDisposition, DuplicateScore


@dataclass(frozen=True)
class DuplicateResolution:
    left_evidence_id: int
    right_evidence_id: int
    canonical_expense_pk: Optional[int]
    resolution_status: str


@dataclass
class PostgresReceiptDuplicateStore:
    connection: object

    @staticmethod
    def _ordered_pair(left_evidence_id: int, right_evidence_id: int) -> tuple[int, int]:
        if left_evidence_id == right_evidence_id:
            raise ValueError("duplicate relationship requires two different evidence records")
        return tuple(sorted((left_evidence_id, right_evidence_id)))

    def save_score(self, result: DuplicateScore) -> None:
        left_id, right_id = self._ordered_pair(result.left_evidence_id, result.right_evidence_id)
        sql = """
            INSERT INTO budget.receipt_duplicate_links (
                left_evidence_id, right_evidence_id, score, disposition,
                reasons, item_similarity, resolution_status, updated_at
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'pending', NOW())
            ON CONFLICT (left_evidence_id, right_evidence_id) DO UPDATE SET
                score = EXCLUDED.score,
                disposition = EXCLUDED.disposition,
                reasons = EXCLUDED.reasons,
                item_similarity = EXCLUDED.item_similarity,
                updated_at = NOW()
        """
        with self.connection.cursor() as cur:
            cur.execute(
                sql,
                (
                    left_id,
                    right_id,
                    result.score,
                    result.disposition.value,
                    json.dumps(result.reasons),
                    result.item_similarity,
                ),
            )
        self.connection.commit()

    def auto_link_exact(self, result: DuplicateScore, canonical_expense_pk: int) -> DuplicateResolution:
        if result.disposition is not DuplicateDisposition.EXACT:
            raise ValueError("only exact duplicates may be auto-linked")
        return self._resolve(result, canonical_expense_pk, status="auto_linked", source="deduplication_exact")

    def confirm_link(self, result: DuplicateScore, canonical_expense_pk: int) -> DuplicateResolution:
        if result.disposition is DuplicateDisposition.DISTINCT:
            raise ValueError("distinct evidence cannot be confirmed as duplicate")
        return self._resolve(result, canonical_expense_pk, status="confirmed", source="manual_review")

    def reject(self, result: DuplicateScore) -> DuplicateResolution:
        left_id, right_id = self._ordered_pair(result.left_evidence_id, result.right_evidence_id)
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE budget.receipt_duplicate_links
                   SET canonical_expense_pk = NULL,
                       resolution_status = 'rejected',
                       resolution_source = 'manual_review',
                       resolved_at = NOW(),
                       updated_at = NOW()
                 WHERE left_evidence_id = %s AND right_evidence_id = %s
                """,
                (left_id, right_id),
            )
        self.connection.commit()
        return DuplicateResolution(left_id, right_id, None, "rejected")

    def _resolve(
        self,
        result: DuplicateScore,
        canonical_expense_pk: int,
        *,
        status: str,
        source: str,
    ) -> DuplicateResolution:
        left_id, right_id = self._ordered_pair(result.left_evidence_id, result.right_evidence_id)
        with self.connection.cursor() as cur:
            # Linking evidence to one existing canonical expense prevents duplicate
            # budget spending while leaving both immutable source documents intact.
            cur.execute(
                """
                UPDATE budget.receipt_evidence
                   SET expense_pk = %s,
                       updated_at = NOW()
                 WHERE id IN (%s, %s)
                """,
                (canonical_expense_pk, left_id, right_id),
            )
            cur.execute(
                """
                INSERT INTO budget.receipt_duplicate_links (
                    left_evidence_id, right_evidence_id, canonical_expense_pk,
                    score, disposition, reasons, item_similarity,
                    resolution_status, resolution_source, resolved_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (left_evidence_id, right_evidence_id) DO UPDATE SET
                    canonical_expense_pk = EXCLUDED.canonical_expense_pk,
                    score = EXCLUDED.score,
                    disposition = EXCLUDED.disposition,
                    reasons = EXCLUDED.reasons,
                    item_similarity = EXCLUDED.item_similarity,
                    resolution_status = EXCLUDED.resolution_status,
                    resolution_source = EXCLUDED.resolution_source,
                    resolved_at = NOW(),
                    updated_at = NOW()
                """,
                (
                    left_id,
                    right_id,
                    canonical_expense_pk,
                    result.score,
                    result.disposition.value,
                    json.dumps(result.reasons),
                    result.item_similarity,
                    status,
                    source,
                ),
            )
        self.connection.commit()
        return DuplicateResolution(left_id, right_id, canonical_expense_pk, status)
