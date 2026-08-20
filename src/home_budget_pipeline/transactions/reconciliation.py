"""Deterministic matching of financial transactions to receipt/canonical evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from enum import Enum
from typing import Iterable, Optional


class MatchOutcome(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class MatchCandidate:
    expense_pk: int
    merchant: Optional[str]
    transaction_date: Optional[date]
    amount: Optional[Decimal]
    card_last4: Optional[str] = None
    account_id: Optional[int] = None
    receipt_id: Optional[str] = None
    order_id: Optional[str] = None


@dataclass(frozen=True)
class TransactionForMatching:
    transaction_id: int
    transaction_date: date
    amount: Decimal
    merchant_text: str
    card_last4: Optional[str] = None
    account_id: Optional[int] = None
    source_transaction_id: Optional[str] = None


@dataclass(frozen=True)
class CandidateScore:
    candidate: MatchCandidate
    score: int
    amount_match: bool
    date_distance_days: Optional[int]
    merchant_similarity: float
    card_match: bool
    account_match: bool
    identifier_match: bool


@dataclass(frozen=True)
class MatchDecision:
    outcome: MatchOutcome
    transaction_id: int
    matched_expense_pk: Optional[int]
    score: Optional[int]
    candidates: tuple[CandidateScore, ...]


@dataclass(frozen=True)
class ReconciliationSummary:
    matched: int
    ambiguous: int
    unmatched: int
    duplicate_imports: int = 0


class TransactionReceiptMatcher:
    """Score candidates conservatively and auto-link only when clearly unique."""

    AUTO_MATCH_THRESHOLD = 80
    AMBIGUITY_MARGIN = 10

    def score(
        self,
        transaction: TransactionForMatching,
        candidate: MatchCandidate,
    ) -> CandidateScore:
        score = 0

        amount_match = candidate.amount is not None and candidate.amount == transaction.amount
        if amount_match:
            score += 45

        date_distance_days: Optional[int] = None
        if candidate.transaction_date is not None:
            date_distance_days = abs((candidate.transaction_date - transaction.transaction_date).days)
            if date_distance_days == 0:
                score += 20
            elif date_distance_days == 1:
                score += 10
            elif date_distance_days <= 3:
                score += 5

        merchant_similarity = self._similarity(transaction.merchant_text, candidate.merchant)
        if merchant_similarity >= 0.90:
            score += 20
        elif merchant_similarity >= 0.75:
            score += 10
        elif merchant_similarity >= 0.55:
            score += 5

        card_match = bool(
            transaction.card_last4
            and candidate.card_last4
            and transaction.card_last4 == candidate.card_last4
        )
        if card_match:
            score += 10

        account_match = bool(
            transaction.account_id is not None
            and candidate.account_id is not None
            and transaction.account_id == candidate.account_id
        )
        if account_match:
            score += 10

        tx_identifier = self._norm(transaction.source_transaction_id)
        identifier_match = bool(
            tx_identifier
            and tx_identifier in {
                self._norm(candidate.receipt_id),
                self._norm(candidate.order_id),
            }
        )
        if identifier_match:
            score += 30

        return CandidateScore(
            candidate=candidate,
            score=score,
            amount_match=amount_match,
            date_distance_days=date_distance_days,
            merchant_similarity=merchant_similarity,
            card_match=card_match,
            account_match=account_match,
            identifier_match=identifier_match,
        )

    def decide(
        self,
        transaction: TransactionForMatching,
        candidates: Iterable[MatchCandidate],
    ) -> MatchDecision:
        scored = tuple(
            sorted(
                (self.score(transaction, c) for c in candidates),
                key=lambda s: (-s.score, s.candidate.expense_pk),
            )
        )
        if not scored or scored[0].score < self.AUTO_MATCH_THRESHOLD:
            return MatchDecision(MatchOutcome.UNMATCHED, transaction.transaction_id, None, None, scored)

        best = scored[0]
        if len(scored) > 1 and scored[1].score >= best.score - self.AMBIGUITY_MARGIN:
            return MatchDecision(MatchOutcome.AMBIGUOUS, transaction.transaction_id, None, best.score, scored)

        return MatchDecision(
            MatchOutcome.MATCHED,
            transaction.transaction_id,
            best.candidate.expense_pk,
            best.score,
            scored,
        )

    def summarize(
        self,
        decisions: Iterable[MatchDecision],
        *,
        duplicate_imports: int = 0,
    ) -> ReconciliationSummary:
        counts = {outcome: 0 for outcome in MatchOutcome}
        for decision in decisions:
            counts[decision.outcome] += 1
        return ReconciliationSummary(
            matched=counts[MatchOutcome.MATCHED],
            ambiguous=counts[MatchOutcome.AMBIGUOUS],
            unmatched=counts[MatchOutcome.UNMATCHED],
            duplicate_imports=duplicate_imports,
        )

    @classmethod
    def _similarity(cls, a: Optional[str], b: Optional[str]) -> float:
        na, nb = cls._norm(a), cls._norm(b)
        if not na or not nb:
            return 0.0
        return SequenceMatcher(None, na, nb).ratio()

    @staticmethod
    def _norm(value: Optional[str]) -> str:
        if not value:
            return ""
        return " ".join(value.lower().strip().split())
