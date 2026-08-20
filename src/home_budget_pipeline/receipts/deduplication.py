"""Cross-source receipt duplicate scoring for KAN-77."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional


class DuplicateDisposition(str, Enum):
    EXACT = "exact_duplicate"
    PROBABLE = "probable_duplicate"
    REVIEW = "review"
    DISTINCT = "distinct"


@dataclass(frozen=True)
class ReceiptItemFingerprint:
    name: str
    quantity: Optional[Decimal] = None
    line_total: Optional[Decimal] = None


@dataclass(frozen=True)
class ReceiptEvidenceFingerprint:
    evidence_id: int
    source_type: str
    merchant: Optional[str]
    transaction_datetime: Optional[datetime]
    total: Optional[Decimal]
    receipt_id: Optional[str] = None
    order_id: Optional[str] = None
    card_last4: Optional[str] = None
    items: tuple[ReceiptItemFingerprint, ...] = ()


@dataclass(frozen=True)
class DuplicateScore:
    left_evidence_id: int
    right_evidence_id: int
    score: int
    disposition: DuplicateDisposition
    reasons: tuple[str, ...]
    item_similarity: float


class ReceiptDuplicateScorer:
    """Score two receipt evidence records without mutating either source."""

    EXACT_THRESHOLD = 90
    PROBABLE_THRESHOLD = 70
    REVIEW_THRESHOLD = 50

    def score(
        self,
        left: ReceiptEvidenceFingerprint,
        right: ReceiptEvidenceFingerprint,
    ) -> DuplicateScore:
        score = 0
        reasons: list[str] = []

        identifiers_left = {self._norm_id(left.receipt_id), self._norm_id(left.order_id)} - {""}
        identifiers_right = {self._norm_id(right.receipt_id), self._norm_id(right.order_id)} - {""}
        if identifiers_left & identifiers_right:
            score += 45
            reasons.append("identifier_match")

        merchant_match = bool(self._norm_text(left.merchant) and self._norm_text(left.merchant) == self._norm_text(right.merchant))
        if merchant_match:
            score += 15
            reasons.append("merchant_match")

        if left.transaction_datetime and right.transaction_datetime:
            seconds = abs((left.transaction_datetime - right.transaction_datetime).total_seconds())
            if seconds <= 300:
                score += 15
                reasons.append("time_within_5m")
            elif left.transaction_datetime.date() == right.transaction_datetime.date():
                score += 8
                reasons.append("same_day")

        if left.total is not None and right.total is not None:
            delta = abs(left.total - right.total)
            if delta <= Decimal("0.01"):
                score += 20
                reasons.append("total_exact")
            else:
                larger = max(abs(left.total), abs(right.total), Decimal("0.01"))
                relative = delta / larger
                if relative <= Decimal("0.05"):
                    score += 10
                    reasons.append("total_within_5pct")

        if left.card_last4 and right.card_last4 and left.card_last4 == right.card_last4:
            score += 10
            reasons.append("card_match")

        item_similarity = self._item_similarity(left.items, right.items)
        if item_similarity >= 0.90:
            score += 25
            reasons.append("items_near_exact")
        elif item_similarity >= 0.70:
            score += 18
            reasons.append("items_strong_overlap")
        elif item_similarity >= 0.50:
            score += 10
            reasons.append("items_partial_overlap")

        # Cross-source exact-total mismatches (for example Instacart vs Costco)
        # can still be strong duplicates when identifiers/items/date align.
        if score >= self.EXACT_THRESHOLD:
            disposition = DuplicateDisposition.EXACT
        elif score >= self.PROBABLE_THRESHOLD:
            disposition = DuplicateDisposition.PROBABLE
        elif score >= self.REVIEW_THRESHOLD:
            disposition = DuplicateDisposition.REVIEW
        else:
            disposition = DuplicateDisposition.DISTINCT

        return DuplicateScore(
            left_evidence_id=left.evidence_id,
            right_evidence_id=right.evidence_id,
            score=score,
            disposition=disposition,
            reasons=tuple(reasons),
            item_similarity=round(item_similarity, 4),
        )

    def rank_candidates(
        self,
        target: ReceiptEvidenceFingerprint,
        candidates: Iterable[ReceiptEvidenceFingerprint],
    ) -> tuple[DuplicateScore, ...]:
        scored = [self.score(target, candidate) for candidate in candidates if candidate.evidence_id != target.evidence_id]
        return tuple(sorted(scored, key=lambda result: (-result.score, result.right_evidence_id)))

    @classmethod
    def _item_similarity(
        cls,
        left: tuple[ReceiptItemFingerprint, ...],
        right: tuple[ReceiptItemFingerprint, ...],
    ) -> float:
        if not left or not right:
            return 0.0
        left_keys = {cls._item_key(item) for item in left}
        right_keys = {cls._item_key(item) for item in right}
        union = left_keys | right_keys
        if not union:
            return 0.0
        return len(left_keys & right_keys) / len(union)

    @classmethod
    def _item_key(cls, item: ReceiptItemFingerprint) -> tuple[str, Optional[str]]:
        quantity = format(item.quantity.normalize(), "f") if item.quantity is not None else None
        return cls._norm_text(item.name), quantity

    @staticmethod
    def _norm_id(value: Optional[str]) -> str:
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    @staticmethod
    def _norm_text(value: Optional[str]) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())
