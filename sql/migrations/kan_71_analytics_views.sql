-- KAN-71: stable consumer-facing analytics views.
-- These views compose canonical and persisted quality results; they do not
-- recalculate, repair, infer, or recategorize source data.

BEGIN;

CREATE OR REPLACE VIEW budget.analytics_expenses AS
SELECT
    e.id AS expense_pk,
    e.source,
    e.order_id,
    e.order_date,
    e.transaction_datetime,
    e.store_name,
    e.account_id,
    a.account_name,
    e.expense_total,
    e.extraction_status,
    r.item_subtotal_status,
    r.item_subtotal_variance,
    r.receipt_total_status,
    r.receipt_total_variance,
    r.category_splits_status,
    r.category_splits_variance,
    COALESCE(r.requires_review, FALSE) AS reconciliation_requires_review,
    q.data_quality_status,
    q.violation_count AS data_quality_violation_count,
    q.violation_rules AS data_quality_violation_rules,
    COALESCE(q.data_quality_requires_review, FALSE) AS data_quality_requires_review,
    (
        COALESCE(r.requires_review, FALSE)
        OR COALESCE(q.data_quality_requires_review, FALSE)
        OR e.extraction_status <> 'complete'
    ) AS requires_review
FROM budget.expenses e
LEFT JOIN budget.accounts a ON a.id = e.account_id
LEFT JOIN budget.expense_reconciliation_status r ON r.expense_pk = e.id
LEFT JOIN budget.expense_data_quality_status q ON q.expense_pk = e.id;

CREATE OR REPLACE VIEW budget.analytics_expense_items AS
SELECT
    ei.id AS expense_item_id,
    ei.expense_pk,
    e.source,
    e.order_id,
    e.order_date,
    e.transaction_datetime,
    e.store_name,
    e.account_id,
    a.account_name,
    ei.item_name,
    ei.line_total,
    ei.original_line_total,
    COALESCE(ei.budget_category, 'Uncategorized') AS budget_category,
    ec.group_id AS category_group_id,
    cg.group_key AS category_group_key,
    cg.group_name AS category_group_name,
    ei.category_source,
    ei.category_confidence,
    ei.category_requires_review
FROM budget.expense_items ei
JOIN budget.expenses e ON e.id = ei.expense_pk
LEFT JOIN budget.accounts a ON a.id = e.account_id
LEFT JOIN budget.expense_categories ec ON ec.category_name = ei.budget_category
LEFT JOIN budget.category_groups cg ON cg.id = ec.group_id;

CREATE OR REPLACE VIEW budget.analytics_category_spend AS
SELECT
    COALESCE(e.order_date, e.transaction_datetime::date) AS expense_date,
    COALESCE(ei.budget_category, 'Uncategorized') AS budget_category,
    ec.group_id AS category_group_id,
    cg.group_key AS category_group_key,
    cg.group_name AS category_group_name,
    SUM(ei.line_total) AS category_amount,
    COUNT(*) AS item_count,
    COUNT(DISTINCT ei.expense_pk) AS expense_count
FROM budget.expense_items ei
JOIN budget.expenses e ON e.id = ei.expense_pk
LEFT JOIN budget.expense_categories ec ON ec.category_name = ei.budget_category
LEFT JOIN budget.category_groups cg ON cg.id = ec.group_id
GROUP BY
    COALESCE(e.order_date, e.transaction_datetime::date),
    COALESCE(ei.budget_category, 'Uncategorized'),
    ec.group_id,
    cg.group_key,
    cg.group_name;

CREATE OR REPLACE VIEW budget.analytics_review_queue AS
SELECT
    ae.expense_pk,
    ae.source,
    ae.order_id,
    ae.order_date,
    ae.transaction_datetime,
    ae.store_name,
    ae.expense_total,
    ae.extraction_status,
    ae.item_subtotal_status,
    ae.item_subtotal_variance,
    ae.receipt_total_status,
    ae.receipt_total_variance,
    ae.category_splits_status,
    ae.category_splits_variance,
    ae.reconciliation_requires_review,
    ae.data_quality_status,
    ae.data_quality_violation_count,
    ae.data_quality_violation_rules,
    ae.data_quality_requires_review,
    ae.requires_review
FROM budget.analytics_expenses ae
WHERE ae.requires_review = TRUE;

COMMIT;
