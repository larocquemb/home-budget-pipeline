from datetime import date
from decimal import Decimal

from home_budget_pipeline.transactions.reconciliation import (
    MatchCandidate,
    MatchOutcome,
    TransactionForMatching,
    TransactionReceiptMatcher,
)


def tx(**overrides):
    values = dict(
        transaction_id=1,
        transaction_date=date(2026, 8, 20),
        amount=Decimal("42.50"),
        merchant_text="Costco Wholesale",
        card_last4="1234",
        account_id=7,
        source_transaction_id=None,
    )
    values.update(overrides)
    return TransactionForMatching(**values)


def candidate(pk=10, **overrides):
    values = dict(
        expense_pk=pk,
        merchant="Costco Wholesale",
        transaction_date=date(2026, 8, 20),
        amount=Decimal("42.50"),
        card_last4="1234",
        account_id=7,
    )
    values.update(overrides)
    return MatchCandidate(**values)


def test_high_confidence_unique_candidate_auto_matches():
    decision = TransactionReceiptMatcher().decide(tx(), [candidate()])
    assert decision.outcome is MatchOutcome.MATCHED
    assert decision.matched_expense_pk == 10
    assert decision.score is not None and decision.score >= 80


def test_same_merchant_same_day_equal_candidates_are_ambiguous():
    decision = TransactionReceiptMatcher().decide(tx(), [candidate(10), candidate(11)])
    assert decision.outcome is MatchOutcome.AMBIGUOUS
    assert decision.matched_expense_pk is None


def test_amount_mismatch_prevents_auto_link():
    decision = TransactionReceiptMatcher().decide(
        tx(), [candidate(amount=Decimal("49.99"), card_last4=None, account_id=None)]
    )
    assert decision.outcome is MatchOutcome.UNMATCHED


def test_identifier_match_can_disambiguate_close_candidates():
    transaction = tx(source_transaction_id="ORDER-99")
    decision = TransactionReceiptMatcher().decide(
        transaction,
        [
            candidate(10, order_id="ORDER-99"),
            candidate(11, merchant="Costco", card_last4=None, account_id=None),
        ],
    )
    assert decision.outcome is MatchOutcome.MATCHED
    assert decision.matched_expense_pk == 10


def test_one_day_date_drift_can_still_match_with_other_strong_signals():
    decision = TransactionReceiptMatcher().decide(
        tx(), [candidate(transaction_date=date(2026, 8, 19))]
    )
    assert decision.outcome is MatchOutcome.MATCHED


def test_summary_reports_match_outcome_counts():
    matcher = TransactionReceiptMatcher()
    decisions = [
        matcher.decide(tx(transaction_id=1), [candidate(10)]),
        matcher.decide(tx(transaction_id=2), [candidate(10), candidate(11)]),
        matcher.decide(tx(transaction_id=3), []),
    ]
    summary = matcher.summarize(decisions)
    assert summary.matched == 1
    assert summary.ambiguous == 1
    assert summary.unmatched == 1
