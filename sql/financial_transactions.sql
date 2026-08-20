-- KAN-78: normalized financial transaction ledger for fresh database bootstrap.

BEGIN;

CREATE TABLE budget.account_ownerships (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES budget.accounts(id) ON DELETE CASCADE,
    owner_name TEXT NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE UNIQUE INDEX uq_account_ownerships_version
    ON budget.account_ownerships (account_id, owner_name, valid_from);
CREATE INDEX idx_account_ownerships_lookup
    ON budget.account_ownerships (account_id, valid_from, valid_to);

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
    payer TEXT,
    payer_source TEXT CHECK (payer_source IS NULL OR payer_source IN ('payment_card', 'account')),
    import_fingerprint TEXT NOT NULL UNIQUE,
    raw_source JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_financial_transactions_account_date
    ON budget.financial_transactions (account_id, transaction_date DESC);
CREATE INDEX idx_financial_transactions_match
    ON budget.financial_transactions (transaction_date, amount, lower(COALESCE(merchant_text, description)));

COMMIT;
