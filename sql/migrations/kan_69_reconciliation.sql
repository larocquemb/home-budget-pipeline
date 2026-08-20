BEGIN;

ALTER TABLE budget.expenses
    DROP COLUMN IF EXISTS total_recon_diff;

ALTER TABLE budget.expenses
    ADD COLUMN total_recon_diff NUMERIC(12, 2) GENERATED ALWAYS AS (
        COALESCE(receipt_item_subtotal, 0)
        + COALESCE(receipt_tip, 0)
        + COALESCE(receipt_service_fee, 0)
        + COALESCE(receipt_recycling_fee, 0)
        + COALESCE(receipt_service_fee_tax, 0)
        + COALESCE(receipt_gst, 0)
        + COALESCE(receipt_pst, 0)
        - COALESCE(receipt_discount_total, 0)
        - COALESCE(expense_total, 0)
    ) STORED;

CREATE TABLE IF NOT EXISTS budget.expense_reconciliation_results (
    expense_pk BIGINT PRIMARY KEY REFERENCES budget.expenses(id) ON DELETE CASCADE,
    item_subtotal_status TEXT NOT NULL
        CHECK (item_subtotal_status IN ('pass', 'fail', 'not_checkable')),
    item_subtotal_variance NUMERIC(12, 2),
    receipt_total_status TEXT NOT NULL
        CHECK (receipt_total_status IN ('pass', 'fail', 'not_checkable')),
    receipt_total_variance NUMERIC(12, 2),
    category_splits_status TEXT NOT NULL
        CHECK (category_splits_status IN ('pass', 'fail', 'not_checkable')),
    category_splits_variance NUMERIC(12, 2),
    requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_expense_reconciliation_review
    ON budget.expense_reconciliation_results (requires_review)
    WHERE requires_review = TRUE;

CREATE OR REPLACE VIEW budget.expense_reconciliation_status AS
SELECT
    e.id AS expense_pk,
    e.source,
    e.order_id,
    e.order_date,
    e.store_name,
    r.item_subtotal_status,
    r.item_subtotal_variance,
    r.receipt_total_status,
    r.receipt_total_variance,
    r.category_splits_status,
    r.category_splits_variance,
    r.requires_review,
    r.reconciled_at
FROM budget.expenses e
LEFT JOIN budget.expense_reconciliation_results r
    ON r.expense_pk = e.id;

COMMIT;
