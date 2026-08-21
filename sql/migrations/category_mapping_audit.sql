BEGIN;

CREATE TABLE IF NOT EXISTS budget.expense_category_mapping_audit (
    id BIGSERIAL PRIMARY KEY,
    mapping_id BIGINT REFERENCES budget.expense_category_mappings(id) ON DELETE SET NULL,
    expense_item_id BIGINT,
    actor_user TEXT NOT NULL,
    actor_email TEXT,
    action TEXT NOT NULL CHECK (action IN ('created', 'changed', 'disabled')),
    item_name TEXT NOT NULL,
    match_text TEXT NOT NULL,
    source TEXT,
    merchant TEXT,
    old_category TEXT,
    new_category TEXT,
    affected_item_count INTEGER NOT NULL DEFAULT 0 CHECK (affected_item_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_expense_category_mapping_audit_mapping
    ON budget.expense_category_mapping_audit (mapping_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_expense_category_mapping_audit_item
    ON budget.expense_category_mapping_audit (expense_item_id, created_at DESC);

COMMIT;
