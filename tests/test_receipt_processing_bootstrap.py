from pathlib import Path


def test_receipt_processing_sql_defines_disposable_ingest_state():
    sql = Path("sql/receipt_processing.sql").read_text(encoding="utf-8")
    assert "CREATE SCHEMA IF NOT EXISTS ingest" in sql
    assert "CREATE TABLE ingest.receipts" in sql
    assert "CREATE TABLE ingest.receipt_processing_status" in sql
    assert "source_sha256 TEXT PRIMARY KEY" in sql
    assert "source_reference TEXT NOT NULL" in sql
    assert "review_required" in sql
    assert "failed" in sql
    assert "attempts INTEGER" in sql


def test_receipt_processing_sql_exposes_budget_read_view():
    sql = Path("sql/receipt_processing.sql").read_text(encoding="utf-8")
    assert "CREATE VIEW budget.receipt_processing_status" in sql
    assert "JOIN ingest.receipts r USING (source_sha256)" in sql


def test_postgres_bootstrap_stages_constraints_and_receipt_processing_sql():
    manifest = Path("k8s/postgres.yaml").read_text(encoding="utf-8")
    assert "schema_constraints.sql" in manifest
    assert "/schema/006-schema-constraints.sql" in manifest
    assert "receipt_processing.sql" in manifest
    assert "/schema/007-receipt-processing.sql" in manifest


def test_processing_status_has_index_for_operational_review():
    sql = Path("sql/receipt_processing.sql").read_text(encoding="utf-8")
    assert "idx_receipt_processing_status_status" in sql
    assert "status, last_attempted_at DESC" in sql
