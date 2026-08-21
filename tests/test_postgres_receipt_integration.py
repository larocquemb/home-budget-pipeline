import os
from decimal import Decimal
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


def test_identical_receipt_lines_are_preserved():
    import psycopg

    receipt = ingest.ScannedReceipt(
        path="duplicate-lines.pdf",
        source_reference="integration/duplicate-lines.pdf",
        source_sha256="integration-duplicate-lines-001",
        merchant="Sobeys",
        transaction_date="2026-08-20",
        receipt_id="integration-r2",
        subtotal=10.98,
        tax=0.0,
        total=10.98,
        items=[
            ingest.ScannedItem("CGOLD HASH BROWNS", 5.49),
            ingest.ScannedItem("CGOLD HASH BROWNS", 5.49),
        ],
        text="Sobeys\nCGOLD HASH BROWNS 5.49\nCGOLD HASH BROWNS 5.49\nTOTAL 10.98",
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
                SELECT item_name, unit_qty, unit_cost, line_total
                FROM budget.expense_items
                WHERE expense_pk = %s
                ORDER BY id
                """,
                (expense_pk,),
            )
            rows = cur.fetchall()

    expected = (
        "CGOLD HASH BROWNS",
        Decimal("1.000"),
        Decimal("5.49"),
        Decimal("5.49"),
    )
    assert rows == [expected, expected]
