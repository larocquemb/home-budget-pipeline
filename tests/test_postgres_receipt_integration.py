import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from home_budget_pipeline.receipts import ingest
from home_budget_pipeline.web.queries import LedgerQueryService


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


def test_web_category_override_creates_rule_and_updates_scoped_exact_matches():
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO budget.category_groups (group_key, group_name)
                VALUES ('household', 'Household')
                RETURNING id
                """
            )
            group_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO budget.expense_categories (category_name, group_id)
                VALUES ('Groceries', %s), ('Indoor Supplies', %s)
                """,
                (group_id, group_id),
            )
            cur.execute(
                """
                INSERT INTO budget.expenses (source, order_id, store_name)
                VALUES ('costco', 'override-costco-1', 'Costco'),
                       ('costco', 'override-costco-2', 'Costco'),
                       ('scanned', 'override-scanned-1', 'Other Store')
                RETURNING id
                """
            )
            expense_ids = [row[0] for row in cur.fetchall()]
            cur.execute(
                """
                INSERT INTO budget.expense_items (
                    expense_pk, item_name, budget_category, category_source, line_total
                )
                VALUES (%s, 'BATH-TOWEL', 'Groceries', 'unresolved', 19.99),
                       (%s, 'Bath Towel', 'Groceries', 'unresolved', 24.99),
                       (%s, 'Bath Towel', 'Groceries', 'unresolved', 14.99)
                RETURNING id
                """,
                tuple(expense_ids),
            )
            selected_item_id = cur.fetchone()[0]
        conn.commit()

    service = LedgerQueryService(
        connect=lambda: psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row)
    )
    result = service.save_category_override(
        selected_item_id,
        'Indoor Supplies',
        actor_user='Paul',
        actor_email='paul@example.com',
    )

    assert result['affected_items'] == 2
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, merchant, match_type, match_text, category_name,
                       provenance, is_approved
                  FROM budget.expense_category_mappings
                 WHERE id = %s
                """,
                (result['mapping_id'],),
            )
            assert cur.fetchone() == (
                'costco', 'Costco', 'exact', 'bath towel', 'Indoor Supplies',
                'manual', True,
            )
            cur.execute(
                """
                SELECT mapping_id, expense_item_id, actor_user, actor_email, action,
                       item_name, match_text, source, merchant, old_category,
                       new_category, affected_item_count
                  FROM budget.expense_category_mapping_audit
                 WHERE id = %s
                """,
                (result['audit_id'],),
            )
            assert cur.fetchone() == (
                result['mapping_id'], selected_item_id, 'Paul', 'paul@example.com',
                'created', 'BATH-TOWEL', 'bath towel', 'costco', 'Costco',
                'Groceries', 'Indoor Supplies', 2,
            )
            cur.execute(
                """
                SELECT budget_category, category_source, category_requires_review
                  FROM budget.expense_items
                 ORDER BY id
                """
            )
            assert cur.fetchall()[-3:] == [
                ('Indoor Supplies', 'rule', False),
                ('Indoor Supplies', 'rule', False),
                ('Groceries', 'unresolved', False),
            ]


def test_web_rule_manager_creates_applies_disables_and_audits_rule():
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO budget.category_groups (group_key, group_name) VALUES ('managed', 'Managed') RETURNING id"
            )
            group_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO budget.expense_categories (category_name, group_id) VALUES ('Managed Category', %s)",
                (group_id,),
            )
            cur.execute(
                "INSERT INTO budget.expenses (source, order_id, store_name) VALUES ('costco', 'managed-rule-1', 'Costco') RETURNING id"
            )
            expense_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO budget.expense_items (expense_pk, item_name, line_total) VALUES (%s, 'BLUE MANAGED WIDGET', 9.99)",
                (expense_id,),
            )
        conn.commit()

    service = LedgerQueryService(connect=lambda: psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row))
    created = service.save_category_rule(
        rule_id=None, source='costco', merchant='Costco', match_type='contains',
        match_text='managed widget', category='Managed Category', priority=20,
        is_active=True, actor_user='Paul', actor_email='paul@example.com',
    )
    assert created['affected_items'] == 1

    disabled = service.save_category_rule(
        rule_id=created['mapping_id'], source='costco', merchant='Costco',
        match_type='contains', match_text='managed widget', category='Managed Category',
        priority=20, is_active=False, actor_user='Paul', actor_email='paul@example.com',
    )
    assert disabled['affected_items'] == 0

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_active FROM budget.expense_category_mappings WHERE id = %s", (created['mapping_id'],))
            assert cur.fetchone() == (False,)
            cur.execute(
                "SELECT action FROM budget.expense_category_mapping_audit WHERE mapping_id = %s ORDER BY id",
                (created['mapping_id'],),
            )
            assert cur.fetchall() == [('created',), ('disabled',)]
