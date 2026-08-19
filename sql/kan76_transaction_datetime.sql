BEGIN;

ALTER TABLE budget.expenses
    ADD COLUMN IF NOT EXISTS transaction_datetime TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_expenses_store_datetime
    ON budget.expenses (store_name, transaction_datetime DESC);

COMMENT ON COLUMN budget.expenses.transaction_datetime IS
    'Receipt-local transaction date/time extracted from source evidence. No timezone is assumed from OCR.';

COMMIT;
