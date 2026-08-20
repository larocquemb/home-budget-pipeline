from decimal import Decimal

import pytest

from home_budget_pipeline.receipts.canonical_items import (
    CanonicalItem,
    CanonicalItemResolver,
    ItemSourceCandidate,
    PostgresCanonicalItemStore,
)


def candidate(evidence_id, source_type, confidence, names):
    return ItemSourceCandidate(
        evidence_id=evidence_id,
        source_type=source_type,
        extraction_confidence=confidence,
        items=tuple(CanonicalItem(name, Decimal("1"), line_total=Decimal("1.00")) for name in names),
    )


def test_reliable_electronic_source_beats_scan():
    resolution = CanonicalItemResolver().resolve([
        candidate(1, "scanned", 0.99, ["Milk", "Eggs", "Bread"]),
        candidate(2, "costco_e_receipt", 0.95, ["Milk", "Eggs"]),
    ])
    assert resolution.evidence_id == 2
    assert resolution.reason == "preferred_structured_source"


def test_low_confidence_structured_source_falls_back_to_reliable_scan():
    resolution = CanonicalItemResolver().resolve([
        candidate(1, "scanned", 0.94, ["Milk", "Eggs", "Bread"]),
        candidate(2, "electronic", 0.45, ["Milk"]),
    ])
    assert resolution.evidence_id == 1
    assert resolution.reason == "scanned_fallback"


def test_empty_structured_source_is_ignored():
    resolution = CanonicalItemResolver().resolve([
        candidate(1, "scanned", 0.90, ["Milk"]),
        candidate(2, "electronic", 0.99, []),
    ])
    assert resolution.evidence_id == 1


def test_no_items_raises():
    with pytest.raises(ValueError, match="must contain items"):
        CanonicalItemResolver().resolve([candidate(1, "scanned", 0.9, [])])


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


def test_apply_replaces_canonical_items_and_upserts_provenance():
    connection = FakeConnection()
    resolution = CanonicalItemResolver().resolve([
        candidate(2, "electronic", 0.95, ["Milk", "Eggs"]),
    ])
    PostgresCanonicalItemStore(connection).apply(42, resolution)

    statements = [sql for sql, _ in connection.cursor_obj.executed]
    assert statements[0].startswith("DELETE FROM budget.expense_items")
    assert sum("INSERT INTO budget.expense_items" in sql for sql in statements) == 2
    assert "ON CONFLICT (expense_pk) DO UPDATE" in statements[-1]
    assert connection.commit_count == 1


def test_schema_persists_canonical_item_source_resolution():
    from pathlib import Path
    text = Path("sql/receipt_deduplication.sql").read_text()
    assert "CREATE TABLE budget.canonical_item_source_resolutions" in text
    assert "evidence_id BIGINT NOT NULL" in text
    assert "reason TEXT NOT NULL" in text
