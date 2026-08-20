from decimal import Decimal

import pytest

from home_budget_pipeline.receipts.deduplication import DuplicateDisposition, DuplicateScore
from home_budget_pipeline.receipts.deduplication_store import PostgresReceiptDuplicateStore


class FakeCursor:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed.append((" ".join(sql.split()), params))


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commit_count = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1


def duplicate_score(disposition=DuplicateDisposition.EXACT):
    return DuplicateScore(
        left_evidence_id=9,
        right_evidence_id=4,
        score=95 if disposition is DuplicateDisposition.EXACT else 72,
        disposition=disposition,
        reasons=("merchant_match", "items_near_exact"),
        item_similarity=0.95,
    )


def test_save_score_is_idempotent_and_orders_pair():
    connection = FakeConnection()
    PostgresReceiptDuplicateStore(connection).save_score(duplicate_score())

    sql, params = connection.cursor_obj.executed[0]
    assert "ON CONFLICT (left_evidence_id, right_evidence_id) DO UPDATE" in sql
    assert params[0:2] == (4, 9)
    assert connection.commit_count == 1


def test_exact_duplicate_can_auto_link_both_evidence_rows_to_one_expense():
    connection = FakeConnection()
    resolution = PostgresReceiptDuplicateStore(connection).auto_link_exact(duplicate_score(), 42)

    update_sql, update_params = connection.cursor_obj.executed[0]
    assert "UPDATE budget.receipt_evidence" in update_sql
    assert "WHERE id IN (%s, %s)" in update_sql
    assert update_params == (42, 4, 9)
    assert resolution.canonical_expense_pk == 42
    assert resolution.resolution_status == "auto_linked"


def test_probable_duplicate_requires_manual_confirmation_for_linking():
    connection = FakeConnection()
    store = PostgresReceiptDuplicateStore(connection)
    result = duplicate_score(DuplicateDisposition.PROBABLE)

    with pytest.raises(ValueError, match="only exact duplicates"):
        store.auto_link_exact(result, 42)

    confirmed = store.confirm_link(result, 42)
    assert confirmed.resolution_status == "confirmed"


def test_distinct_evidence_cannot_be_confirmed_as_duplicate():
    connection = FakeConnection()
    result = duplicate_score(DuplicateDisposition.DISTINCT)

    with pytest.raises(ValueError, match="distinct evidence"):
        PostgresReceiptDuplicateStore(connection).confirm_link(result, 42)


def test_reject_marks_existing_candidate_without_deleting_evidence():
    connection = FakeConnection()
    resolution = PostgresReceiptDuplicateStore(connection).reject(
        duplicate_score(DuplicateDisposition.REVIEW)
    )

    sql, params = connection.cursor_obj.executed[0]
    assert "resolution_status = 'rejected'" in sql
    assert "DELETE" not in sql
    assert params == (4, 9)
    assert resolution.canonical_expense_pk is None
