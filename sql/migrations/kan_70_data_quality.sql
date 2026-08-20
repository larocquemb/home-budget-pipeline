BEGIN;

CREATE TABLE IF NOT EXISTS budget.expense_data_quality_results (
    expense_pk BIGINT PRIMARY KEY REFERENCES budget.expenses(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'not_applicable')),
    requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS budget.expense_data_quality_violations (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT NOT NULL REFERENCES budget.expenses(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    field_name TEXT NOT NULL,
    item_id BIGINT REFERENCES budget.expense_items(id) ON DELETE CASCADE,
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_expense_data_quality_review
    ON budget.expense_data_quality_results (requires_review)
    WHERE requires_review = TRUE;

CREATE INDEX IF NOT EXISTS idx_expense_data_quality_violations_expense
    ON budget.expense_data_quality_violations (expense_pk, rule);

CREATE OR REPLACE VIEW budget.expense_data_quality_status AS
SELECT
    e.id AS expense_pk,
    q.status AS data_quality_status,
    q.requires_review AS data_quality_requires_review,
    q.validated_at,
    COALESCE(v.violation_count, 0) AS violation_count,
    COALESCE(v.rules, ARRAY[]::TEXT[]) AS violation_rules
FROM budget.expenses e
LEFT JOIN budget.expense_data_quality_results q ON q.expense_pk = e.id
LEFT JOIN (
    SELECT
        expense_pk,
        COUNT(*) AS violation_count,
        ARRAY_AGG(rule ORDER BY id) AS rules
    FROM budget.expense_data_quality_violations
    GROUP BY expense_pk
) v ON v.expense_pk = e.id;

COMMIT;
