-- KAN-68: canonical category mapping persistence and category splits
-- Apply after sql/schema_phase1.sql.

BEGIN;

ALTER TABLE budget.expense_category_mappings
    ADD COLUMN IF NOT EXISTS provenance TEXT NOT NULL DEFAULT 'rule',
    ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_expense_category_mappings_provenance'
          AND conrelid = 'budget.expense_category_mappings'::regclass
    ) THEN
        ALTER TABLE budget.expense_category_mappings
            ADD CONSTRAINT ck_expense_category_mappings_provenance
            CHECK (provenance IN ('rule', 'learned_mapping', 'ai', 'manual'));
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_expense_category_mappings_approved
    ON budget.expense_category_mappings (is_active, is_approved, provenance, priority, id);

ALTER TABLE budget.expense_items
    ADD COLUMN IF NOT EXISTS category_confidence NUMERIC(5, 4),
    ADD COLUMN IF NOT EXISTS category_rationale TEXT,
    ADD COLUMN IF NOT EXISTS category_requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS categorized_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_expense_items_category_confidence'
          AND conrelid = 'budget.expense_items'::regclass
    ) THEN
        ALTER TABLE budget.expense_items
            ADD CONSTRAINT ck_expense_items_category_confidence
            CHECK (
                category_confidence IS NULL
                OR (category_confidence >= 0 AND category_confidence <= 1)
            );
    END IF;
END;
$$;

CREATE OR REPLACE VIEW budget.expense_category_splits AS
SELECT
    expense_pk,
    budget_category,
    ROUND(SUM(COALESCE(line_total, 0)), 2) AS category_amount,
    COUNT(*) AS item_count,
    BOOL_OR(category_requires_review) AS requires_review
FROM budget.expense_items
WHERE budget_category IS NOT NULL
GROUP BY expense_pk, budget_category;

COMMENT ON VIEW budget.expense_category_splits IS
    'KAN-68 category totals aggregated from canonical line items for later export/reconciliation.';

COMMIT;
