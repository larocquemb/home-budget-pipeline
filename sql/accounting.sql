-- Durable accounting data. Provision in a dedicated database, never receipt bootstrap.
CREATE SCHEMA IF NOT EXISTS accounting;
CREATE TABLE IF NOT EXISTS accounting.accounts (
 id BIGSERIAL PRIMARY KEY,
 account_reference TEXT NOT NULL UNIQUE,
 institution TEXT NOT NULL,
 primary_card_last4 TEXT CHECK (primary_card_last4 IS NULL OR primary_card_last4 ~ '^[0-9]{4}$'),
 account_type TEXT NOT NULL DEFAULT 'credit_card',
 currency CHAR(3) NOT NULL DEFAULT 'CAD',
 archive_root TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS accounting.account_numbers (
 id BIGSERIAL PRIMARY KEY,
 account_id BIGINT NOT NULL REFERENCES accounting.accounts(id),
 number_kind TEXT NOT NULL CHECK (number_kind IN ('bank_account','card')),
 last4 TEXT NOT NULL CHECK (last4 ~ '^[0-9]{4}$'),
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE (account_id,number_kind,last4),
 UNIQUE (account_id,id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_accounting_primary_identifier
 ON accounting.accounts(institution,account_type,primary_card_last4)
 WHERE primary_card_last4 IS NOT NULL;
CREATE TABLE IF NOT EXISTS accounting.account_ownerships (
 id BIGSERIAL PRIMARY KEY,
 account_id BIGINT NOT NULL REFERENCES accounting.accounts(id),
 account_number_id BIGINT,
 owner_name TEXT NOT NULL CHECK(length(trim(owner_name))>0),
 ownership_role TEXT NOT NULL CONSTRAINT account_ownerships_roles_v2 CHECK (ownership_role IN ('primary','secondary')),
 effective_from DATE,
 effective_to DATE,
 asserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 actor TEXT NOT NULL,
 basis TEXT NOT NULL,
 FOREIGN KEY (account_id,account_number_id) REFERENCES accounting.account_numbers(account_id,id),
 CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
 CONSTRAINT account_ownerships_card_reference_v2 CHECK (account_number_id IS NOT NULL),
 UNIQUE NULLS NOT DISTINCT (account_id,account_number_id,owner_name,ownership_role,effective_from)
);

-- Upgrade the initial account-level/cardholder layout without discarding history.
-- NOT VALID allows the ownership operation to retain the old snapshot in audit
-- before replacing canonical rows and validating the new constraints.
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='accounting.account_ownerships'::regclass
               AND conname='account_ownerships_roles_v2') THEN
  ALTER TABLE accounting.account_ownerships DROP CONSTRAINT IF EXISTS account_ownerships_ownership_role_check;
  ALTER TABLE accounting.account_ownerships DROP CONSTRAINT IF EXISTS account_ownerships_check1;
  ALTER TABLE accounting.account_ownerships ADD CONSTRAINT account_ownerships_roles_v2
      CHECK(ownership_role IN ('primary','secondary')) NOT VALID;
  ALTER TABLE accounting.account_ownerships ADD CONSTRAINT account_ownerships_card_reference_v2
      CHECK(account_number_id IS NOT NULL) NOT VALID;
 END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_account_ownership_card
 ON accounting.account_ownerships(account_number_id);
CREATE TABLE IF NOT EXISTS accounting.account_ownership_audit (
 id BIGSERIAL PRIMARY KEY,
 account_id BIGINT NOT NULL REFERENCES accounting.accounts(id),
 actor TEXT NOT NULL,
 reason TEXT NOT NULL,
 before_ownerships JSONB NOT NULL,
 after_ownerships JSONB NOT NULL,
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounting.financial_transactions (
    id BIGSERIAL PRIMARY KEY,
    institution TEXT NOT NULL,
    account_id BIGINT REFERENCES accounting.accounts(id) ON DELETE SET NULL,
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

CREATE INDEX IF NOT EXISTS idx_financial_transactions_account_date
    ON accounting.financial_transactions (account_id, transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_financial_transactions_match
    ON accounting.financial_transactions (transaction_date, amount, lower(COALESCE(merchant_text, description)));


-- Additive bank import provenance and confirmed split allocations.
CREATE TABLE IF NOT EXISTS accounting.transaction_imports (
    id BIGSERIAL PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    account_reference TEXT NOT NULL,
    format TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    actor TEXT NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_sha256, account_reference)
);
CREATE TABLE IF NOT EXISTS accounting.transaction_import_rows (
    import_id BIGINT NOT NULL REFERENCES accounting.transaction_imports(id),
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    transaction_id BIGINT NOT NULL REFERENCES accounting.financial_transactions(id),
    raw_row JSONB NOT NULL,
    PRIMARY KEY (import_id, row_number)
);
CREATE TABLE IF NOT EXISTS accounting.source_artifacts (
    import_id BIGINT NOT NULL REFERENCES accounting.transaction_imports(id),
    source_uri TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    backup_status TEXT NOT NULL DEFAULT 'independent backup unverified',
    PRIMARY KEY (import_id,source_uri)
);
CREATE INDEX IF NOT EXISTS idx_transaction_import_rows_transaction
    ON accounting.transaction_import_rows(transaction_id);
CREATE TABLE IF NOT EXISTS accounting.transaction_allocations (
    transaction_id BIGINT NOT NULL REFERENCES accounting.financial_transactions(id),
    allocation_number INTEGER NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount <> 0),
    category TEXT NOT NULL CHECK (length(trim(category)) > 0),
    person TEXT,
    expense_pk BIGINT, -- Legacy display hint; durable identity is the source hash below.
    receipt_source_sha256 TEXT CHECK (receipt_source_sha256 IS NULL OR receipt_source_sha256 ~ '^[0-9a-f]{64}$'),
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (transaction_id, allocation_number)
);
CREATE TABLE IF NOT EXISTS accounting.transaction_allocation_audit (
    id BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT NOT NULL REFERENCES accounting.financial_transactions(id),
    version INTEGER NOT NULL CHECK (version > 0),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_allocations JSONB NOT NULL,
    after_allocations JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (transaction_id, version)
);

-- A saved split is complete. No rows means unallocated; rows must balance.
-- Deferred checks allow an atomic replace, and parent locks serialize writers.
CREATE OR REPLACE FUNCTION accounting.check_transaction_allocation_balance()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE txid bigint; expected numeric; allocated numeric; n bigint;
BEGIN
    FOR txid IN SELECT DISTINCT x FROM unnest(ARRAY[
        CASE WHEN TG_OP <> 'INSERT' THEN OLD.transaction_id END,
        CASE WHEN TG_OP <> 'DELETE' THEN NEW.transaction_id END]) x WHERE x IS NOT NULL
    LOOP
        SELECT amount INTO expected FROM accounting.financial_transactions WHERE id=txid FOR UPDATE;
        SELECT count(*), sum(amount) INTO n, allocated
          FROM accounting.transaction_allocations WHERE transaction_id=txid;
        IF n > 0 AND (allocated <> expected OR EXISTS (
            SELECT 1 FROM accounting.transaction_allocations
             WHERE transaction_id=txid AND sign(amount) <> sign(expected))) THEN
            RAISE EXCEPTION 'Transaction allocations must balance with consistent signs';
        END IF;
    END LOOP;
    RETURN NULL;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='transaction_allocation_balance'
                   AND tgrelid='accounting.transaction_allocations'::regclass) THEN
        CREATE CONSTRAINT TRIGGER transaction_allocation_balance
        AFTER INSERT OR UPDATE OR DELETE ON accounting.transaction_allocations
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION accounting.check_transaction_allocation_balance();
    END IF;
END $$;
