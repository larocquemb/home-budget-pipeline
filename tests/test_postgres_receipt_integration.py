import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from home_budget_pipeline.receipts import ingest


pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not set", allow_module_level=True)


def test_uncategorized_ocr_item_satisfies_real_postgres_schema():
    import psycopg

    receipt = ingest.ScannedReceipt(
        path="receipt.pdf",
        source_reference="integration/receipt.pdf",
        source_sha256="integration-category-source-001",
        merchant="Sobeys",
        transaction_date="2026-08-20",
        receipt_id="integration-r1",
        subtotal=5.0,
        tax=0.0,
        total=5.0,
        items=[ingest.ScannedItem("Milk", 5.0)],
        text="Sobeys\nMilk 5.00\nTOTAL 5.00",
        extraction_confidence=1.0,
        extraction_status="complete",
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
                """
                SELECT budget_category, category_source
                FROM budget.expense_items
                WHERE expense_pk = %s
                """,
                (expense_pk,),
            )
            assert cur.fetchone() == (None, None)
