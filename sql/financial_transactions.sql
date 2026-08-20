-- KAN-78: normalized financial transaction ledger for fresh database bootstrap.

BEGIN;

CREATE TABLE budget.financial_transactions (
    id BIGSERIAL PRIMARY KEY,
    institution TEXT NOT NULL,
    account_id BIGINT REFERENCES budget.accounts(id) ON DELETE SET NULL,
    source_account_reference TEXT NOT NULL,
    source_transaction_id TEXT,
    transaction_date DATE NOT NULL,
    posting_date DATE,
    amount NUMERIC(14, 2) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'CAD',
    description TEXT NOT NULL,
    merchant_text TEXT,
    card_last4 TEXT CHECK (card_last4 IS NULL OR card_last4 ~ '^[0-9]{4}$'),
    import_fingerprint TEXT NOT NULL UNIQUE,
    raw_source JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_financial_transactions_account_date
    ON budget.financial_transactions (account_id, transaction_date DESC);
CREATE INDEX idx_financial_transactions_match
    ON budget.financial_transactions (transaction_date, amount, lower(COALESCE(merchant_text, description)));

COMMIT;
