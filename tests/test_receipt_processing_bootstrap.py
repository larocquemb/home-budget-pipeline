from pathlib import Path


def test_receipt_processing_sql_defines_retry_state_table():
    sql = Path("sql/receipt_processing.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE budget.receipt_processing_status" in sql
    assert "review_required" in sql
    assert "failed" in sql
    assert "attempts INTEGER" in sql


def test_postgres_bootstrap_stages_receipt_processing_sql():
    manifest = Path("k8s/postgres.yaml").read_text(encoding="utf-8")
    assert "receipt_processing.sql" in manifest
    assert "/schema/006-receipt-processing.sql" in manifest


def test_processing_status_has_index_for_operational_review():
    sql = Path("sql/receipt_processing.sql").read_text(encoding="utf-8")
    assert "idx_receipt_processing_status_status" in sql
    assert "status, last_attempted_at DESC" in sql
