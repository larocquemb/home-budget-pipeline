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
        "payer TEXT",
        "payer_source TEXT",
        "import_fingerprint TEXT NOT NULL UNIQUE",
        "raw_source JSONB NOT NULL",
    ):
        assert column in text


def test_effective_dated_account_ownership_contract():
    text = Path("sql/financial_transactions.sql").read_text()
    assert "CREATE TABLE budget.account_ownerships" in text
    assert "account_id BIGINT NOT NULL REFERENCES budget.accounts(id)" in text
    assert "owner_name TEXT NOT NULL" in text
    assert "valid_from DATE NOT NULL" in text
    assert "valid_to DATE" in text
    assert "idx_account_ownerships_lookup" in text


def test_fresh_bootstrap_stages_transaction_ddl_without_migration():
    text = Path("k8s/postgres.yaml").read_text()
    assert "/schema/004-financial-transactions.sql" in text
    assert "sql/financial_transactions.sql" in text
    assert "migrations" not in text
