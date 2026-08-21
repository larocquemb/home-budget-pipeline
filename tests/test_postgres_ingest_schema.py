import os

import pytest


pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not set", allow_module_level=True)


def test_ingest_sha_identity_joins_to_source_reference_and_budget_view():
    import psycopg

    sha = "integration-ingest-sha-001"
    reference = "2026-08-14/receipts_20260814_0007.pdf"

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest.receipts (source_sha256, source_reference)
                VALUES (%s, %s)
                """,
                (sha, reference),
            )
            cur.execute(
                """
                INSERT INTO ingest.receipt_processing_status (
                    source_sha256, status, attempts, first_attempted_at,
                    last_attempted_at, completed_at
                )
                VALUES (%s, 'succeeded', 1, NOW(), NOW(), NOW())
                """,
                (sha,),
            )
            cur.execute(
                """
                SELECT source_sha256, source_reference, status
                  FROM budget.receipt_processing_status
                 WHERE source_sha256 = %s
                """,
                (sha,),
            )
            assert cur.fetchone() == (sha, reference, "succeeded")


def test_repeated_identical_expense_items_are_not_uniquely_constrained():
    import psycopg

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass('budget.uq_expense_items_natural')
                """
            )
            assert cur.fetchone() == (None,)
