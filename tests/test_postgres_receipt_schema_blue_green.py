from pathlib import Path
import os

import pytest

pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not set", allow_module_level=True)

TEMPLATE = Path("sql/receipt_processing_template.sql")


def test_blue_green_receipt_schema_cutover_tracks_version_and_color(monkeypatch, tmp_path):
    import psycopg
    from home_budget_pipeline.receipts import schema_blue_green

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        assert schema_blue_green.ensure_receipt_schema(conn, TEMPLATE) is True

        state = conn.execute(
            """
            SELECT version, active_color, previous_version, is_dirty
              FROM ops.schema_state
             WHERE component = 'receipt_ingest'
            """
        ).fetchone()
        assert state == (1, "green", 0, False)

        kinds = dict(
            conn.execute(
                """
                SELECT c.relname, c.relkind
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'ingest'
                   AND c.relname IN ('receipts', 'receipt_processing_status')
                """
            ).fetchall()
        )
        assert kinds == {"receipts": "v", "receipt_processing_status": "v"}
        assert conn.execute("SELECT to_regclass('ingest_blue.receipts')").fetchone()[0] is not None
        assert conn.execute("SELECT to_regclass('ingest_green.receipts')").fetchone()[0] is not None
        assert schema_blue_green.ensure_receipt_schema(conn, TEMPLATE) is False

        changed_template = tmp_path / "receipt_processing_template.sql"
        changed_template.write_text(
            TEMPLATE.read_text(encoding="utf-8") + "\n-- intentional schema change\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="SCHEMA_VERSION bump"):
            schema_blue_green.ensure_receipt_schema(conn, changed_template)

        monkeypatch.setattr(schema_blue_green, "SCHEMA_VERSION", 2)
        assert schema_blue_green.ensure_receipt_schema(conn, changed_template) is True

        state = conn.execute(
            """
            SELECT version, active_color, previous_version, is_dirty
              FROM ops.schema_state
             WHERE component = 'receipt_ingest'
            """
        ).fetchone()
        assert state == (2, "blue", 1, False)

        # Green remains the rollback copy while blue is active.
        assert conn.execute("SELECT to_regclass('ingest_blue.receipts')").fetchone()[0] is not None
        assert conn.execute("SELECT to_regclass('ingest_green.receipts')").fetchone()[0] is not None


def test_blue_green_template_uses_schema_placeholder():
    sql = TEMPLATE.read_text(encoding="utf-8")
    assert "__INGEST_SCHEMA__" in sql
    assert 'CREATE TABLE "__INGEST_SCHEMA__".receipts' in sql
    assert 'CREATE TABLE "__INGEST_SCHEMA__".receipt_processing_status' in sql
