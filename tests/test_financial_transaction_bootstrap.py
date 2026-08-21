from pathlib import Path


def test_financial_transaction_table_contract():
    text = Path("sql/financial_transactions.sql").read_text()
    assert "CREATE TABLE budget.financial_transactions" in text
    for column in (
        "institution TEXT NOT NULL",
        "source_account_reference TEXT NOT NULL",
        "transaction_date DATE NOT NULL",
        "posting_date DATE",
        "amount NUMERIC(14, 2) NOT NULL",
        "description TEXT NOT NULL",
        "import_fingerprint TEXT NOT NULL UNIQUE",
        "raw_source JSONB NOT NULL",
        "payer TEXT",
        "payer_source TEXT",
    ):
        assert column in text


def test_effective_dated_account_ownership_contract():
    text = Path("sql/financial_transactions.sql").read_text()
    assert "CREATE TABLE budget.account_ownerships" in text
    assert "owner_name TEXT NOT NULL" in text
    assert "valid_from DATE NOT NULL" in text
    assert "valid_to DATE" in text


def test_reconciliation_and_enrichment_persistence_contract():
    text = Path("sql/financial_transactions.sql").read_text()
    assert "CREATE TABLE budget.transaction_expense_reconciliation" in text
    assert "outcome TEXT NOT NULL" in text
    assert "candidate_details JSONB" in text
    assert "CREATE TABLE budget.expense_transaction_enrichment" in text
    assert "transaction_datetime_applied BOOLEAN" in text
    assert "payer_applied BOOLEAN" in text


def test_fresh_bootstrap_stages_transaction_ddl_without_migration():
    manifest = Path("k8s/postgres.yaml").read_text()
    bootstrap = Path("scripts/stage_db_bootstrap.sh").read_text()
    assert "scripts/stage_db_bootstrap.sh /schema" in manifest
    assert "004-financial-transactions.sql" in bootstrap
    assert "sql/financial_transactions.sql" in bootstrap
    assert "migrations" not in bootstrap
