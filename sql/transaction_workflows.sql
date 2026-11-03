-- Additive bank import provenance and confirmed split allocations.
CREATE TABLE IF NOT EXISTS budget.transaction_imports (
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
CREATE TABLE IF NOT EXISTS budget.transaction_import_rows (
    import_id BIGINT NOT NULL REFERENCES budget.transaction_imports(id),
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    transaction_id BIGINT NOT NULL REFERENCES budget.financial_transactions(id),
    raw_row JSONB NOT NULL,
    PRIMARY KEY (import_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_transaction_import_rows_transaction
    ON budget.transaction_import_rows(transaction_id);
CREATE TABLE IF NOT EXISTS budget.transaction_allocations (
    transaction_id BIGINT NOT NULL REFERENCES budget.financial_transactions(id),
    allocation_number INTEGER NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount <> 0),
    category TEXT NOT NULL CHECK (length(trim(category)) > 0),
    person TEXT,
    expense_pk BIGINT REFERENCES budget.expenses(id),
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (transaction_id, allocation_number)
);
CREATE TABLE IF NOT EXISTS budget.transaction_allocation_audit (
    id BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT NOT NULL REFERENCES budget.financial_transactions(id),
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
CREATE OR REPLACE FUNCTION budget.check_transaction_allocation_balance()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE txid bigint; expected numeric; allocated numeric; n bigint;
BEGIN
    FOR txid IN SELECT DISTINCT x FROM unnest(ARRAY[
        CASE WHEN TG_OP <> 'INSERT' THEN OLD.transaction_id END,
        CASE WHEN TG_OP <> 'DELETE' THEN NEW.transaction_id END]) x WHERE x IS NOT NULL
    LOOP
        SELECT amount INTO expected FROM budget.financial_transactions WHERE id=txid FOR UPDATE;
        SELECT count(*), sum(amount) INTO n, allocated
          FROM budget.transaction_allocations WHERE transaction_id=txid;
        IF n > 0 AND (allocated <> expected OR EXISTS (
            SELECT 1 FROM budget.transaction_allocations
             WHERE transaction_id=txid AND sign(amount) <> sign(expected))) THEN
            RAISE EXCEPTION 'Transaction allocations must balance with consistent signs';
        END IF;
    END LOOP;
    RETURN NULL;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='transaction_allocation_balance'
                   AND tgrelid='budget.transaction_allocations'::regclass) THEN
        CREATE CONSTRAINT TRIGGER transaction_allocation_balance
        AFTER INSERT OR UPDATE OR DELETE ON budget.transaction_allocations
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION budget.check_transaction_allocation_balance();
    END IF;
END $$;
