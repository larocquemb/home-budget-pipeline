from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from home_budget_pipeline.receipts import ingest


class FakeCursor:
    def __init__(self):
        self.executed = []
        self._fetchone = (42,)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj


def test_uncategorized_ocr_items_persist_without_category_source():
    receipt = ingest.ScannedReceipt(
        path="receipt.pdf",
        source_reference="receipt.pdf",
        source_sha256="abc123",
        merchant="Sobeys",
        transaction_date="2026-08-20",
        receipt_id="r1",
        subtotal=5.0,
        tax=0.0,
        total=5.0,
        items=[ingest.ScannedItem("Milk", 5.0)],
        text="Sobeys\nMilk 5.00\nTOTAL 5.00",
        extraction_confidence=1.0,
        extraction_status="complete",
    )
    conn = FakeConn()
    payment = SimpleNamespace(payment_method=None, card_last4=None)

    with patch.object(ingest, "extract_payment_provenance", return_value=payment), patch.object(
        ingest, "resolve_owner_from_db", return_value=None
    ):
        ingest.upsert_receipt(conn, receipt)

    item_sql = next(sql for sql, _ in conn.cursor_obj.executed if "INSERT INTO budget.expense_items" in sql)
    assert "category_source" in item_sql
    assert "NULL" in item_sql
    assert "scanned_ocr" not in item_sql
