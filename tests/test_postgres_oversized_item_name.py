import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from home_budget_pipeline.receipts import ingest


pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not set", allow_module_level=True)


def test_oversized_ocr_item_name_does_not_exceed_postgres_btree_limit():
    import psycopg

    oversized_name = "OCR-GARBAGE " * 400
    receipt = ingest.ScannedReceipt(
        path="oversized-item.pdf",
        source_reference="integration/oversized-item.pdf",
        source_sha256="integration-oversized-item-001",
        merchant="Sobeys",
        transaction_date="2026-08-23",
        receipt_id="integration-oversized-item-r1",
        subtotal=5.0,
        tax=0.0,
        total=5.0,
        items=[ingest.ScannedItem(oversized_name, 5.0)],
        text=f"Sobeys\n{oversized_name} 5.00\nTOTAL 5.00",
        extraction_confidence=0.5,
        extraction_status="review",
    )
    payment = SimpleNamespace(payment_method=None, card_last4=None)

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with patch.object(ingest, "extract_payment_provenance", return_value=payment), patch.object(
            ingest, "resolve_owner_from_db", return_value=None
        ):
            expense_pk = ingest.upsert_receipt(conn, receipt)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT length(item_name), item_name_norm = lower(trim(item_name)) "
                "FROM budget.expense_items WHERE expense_pk = %s",
                (expense_pk,),
            )
            item_length, normalized_correctly = cur.fetchone()

    assert item_length > 2704
    assert normalized_correctly is True
