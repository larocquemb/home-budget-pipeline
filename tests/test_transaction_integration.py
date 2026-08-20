from datetime import date, datetime
from decimal import Decimal

import pytest

from home_budget_pipeline.transactions.integration import PostgresTransactionReconciliationStore
from home_budget_pipeline.transactions.reconciliation import (
    MatchCandidate,
    MatchDecision,
    MatchOutcome,
    TransactionForMatching,
    TransactionReceiptMatcher,
)


class FakeCursor:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.responses.pop(0) if self.responses else None


class FakeConnection:
    def __init__(self, responses=None):
        self.cursor_obj = FakeCursor(responses)
        self.commit_count = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1


def matched_decision():
    candidate = MatchCandidate(
        expense_pk=42,
        merchant="Sobeys",
        transaction_date=date(2026, 8, 20),
        amount=Decimal("12.34"),
    )
    transaction = TransactionForMatching(
        transaction_id=9,
        transaction_date=date(2026, 8, 20),
        amount=Decimal("12.34"),
        merchant_text="Sobeys",
    )
    return transaction, TransactionReceiptMatcher().decide(transaction, [candidate])


def test_save_decision_upserts_reconciliation_details():
    transaction, decision = matched_decision()
    connection = FakeConnection()
    PostgresTransactionReconciliationStore(connection).save_decision(decision)

    sql, params = connection.cursor_obj.executed[0]
    assert "ON CONFLICT (transaction_id) DO UPDATE" in sql
    assert params[0:5] == (transaction.transaction_id, 42, "matched", decision.score, 1)
    assert connection.commit_count == 1


def test_enrichment_fills_only_missing_canonical_metadata():
    transaction, decision = matched_decision()
    connection = FakeConnection(responses=[(None, None)])
    result = PostgresTransactionReconciliationStore(connection).enrich_matched_expense(
        decision=decision,
        transaction=transaction,
        payer="Paul",
    )

    assert result.transaction_datetime_applied is True
    assert result.payer_applied is True
    update_sql, update_params = connection.cursor_obj.executed[1]
    assert "COALESCE(transaction_datetime" in update_sql
    assert update_params[0] == datetime(2026, 8, 20, 0, 0)
    assert update_params[1] == "Paul"


def test_enrichment_preserves_existing_canonical_metadata():
    transaction, decision = matched_decision()
    existing_dt = datetime(2026, 8, 20, 16, 15)
    connection = FakeConnection(responses=[(existing_dt, "Roxanne")])
    result = PostgresTransactionReconciliationStore(connection).enrich_matched_expense(
        decision=decision,
        transaction=transaction,
        payer="Paul",
    )

    assert result.transaction_datetime_applied is False
    assert result.payer_applied is False
    provenance_params = connection.cursor_obj.executed[2][1]
    assert provenance_params == (42, 9, False, False)


def test_unmatched_decision_cannot_enrich_expense():
    transaction = TransactionForMatching(9, date(2026, 8, 20), Decimal("12.34"), "Sobeys")
    decision = MatchDecision(MatchOutcome.UNMATCHED, 9, None, None, ())
    with pytest.raises(ValueError, match="only matched"):
        PostgresTransactionReconciliationStore(FakeConnection()).enrich_matched_expense(
            decision=decision,
            transaction=transaction,
            payer="Paul",
        )


def test_summary_reports_duplicate_imports():
    transaction, decision = matched_decision()
    summary = TransactionReceiptMatcher().summarize([decision], duplicate_imports=3)
    assert summary.matched == 1
    assert summary.duplicate_imports == 3


def test_financial_transaction_schema_preserves_enrichment_provenance():
    from pathlib import Path

    text = Path("sql/financial_transactions.sql").read_text()
    assert "CREATE TABLE budget.transaction_expense_reconciliation" in text
    assert "CREATE TABLE budget.expense_transaction_enrichment" in text
    assert "source_type TEXT NOT NULL DEFAULT 'financial_transaction'" in text
