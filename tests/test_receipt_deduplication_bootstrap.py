from pathlib import Path


def test_receipt_duplicate_link_table_contract():
    text = Path("sql/receipt_deduplication.sql").read_text()
    assert "CREATE TABLE budget.receipt_duplicate_links" in text
    assert "left_evidence_id BIGINT NOT NULL" in text
    assert "right_evidence_id BIGINT NOT NULL" in text
    assert "canonical_expense_pk BIGINT" in text
    assert "score INTEGER NOT NULL" in text
    assert "disposition TEXT NOT NULL" in text
    assert "reasons JSONB NOT NULL" in text
    assert "resolution_status TEXT NOT NULL" in text
    assert "CONSTRAINT uq_receipt_duplicate_pair UNIQUE" in text


def test_fresh_bootstrap_stages_receipt_deduplication_without_migration():
    text = Path("k8s/postgres.yaml").read_text()
    assert "sql/receipt_deduplication.sql" in text
    assert "/schema/005-receipt-deduplication.sql" in text
    assert "migrations" not in text
