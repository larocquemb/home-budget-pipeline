BEGIN;
ALTER TABLE budget.expense_items ADD COLUMN IF NOT EXISTS product_description TEXT;
ALTER TABLE budget.expense_items ADD COLUMN IF NOT EXISTS product_url TEXT;
CREATE TABLE IF NOT EXISTS budget.expense_item_description_audit (
    id BIGSERIAL PRIMARY KEY,
    expense_item_id BIGINT REFERENCES budget.expense_items(id) ON DELETE SET NULL,
    actor_user TEXT NOT NULL,
    actor_email TEXT,
    old_description TEXT,
    new_description TEXT,
    old_url TEXT,
    new_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE budget.expense_item_description_audit ADD COLUMN IF NOT EXISTS old_url TEXT;
ALTER TABLE budget.expense_item_description_audit ADD COLUMN IF NOT EXISTS new_url TEXT;
CREATE INDEX IF NOT EXISTS idx_expense_item_description_audit_item
    ON budget.expense_item_description_audit (expense_item_id, created_at DESC);
CREATE TABLE IF NOT EXISTS budget.product_enrichment_results (
    id BIGSERIAL PRIMARY KEY,
    expense_item_id BIGINT NOT NULL UNIQUE REFERENCES budget.expense_items(id) ON DELETE CASCADE,
    provider TEXT NOT NULL, search_query TEXT NOT NULL, candidate_title TEXT,
    candidate_url TEXT, confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'review', 'rejected', 'error')),
    searched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMIT;
