WITH items AS (
    SELECT
        e.id AS receipt_pk,
        e.order_id AS receipt_id,
        e.order_date AS date,
        i.id AS item_pk,
        i.item_name AS item,
        COALESCE(i.unit_qty, i.weight_qty, 0) AS qty,
        i.budget_category,
        i.line_total,
        COALESCE(e.receipt_total_charged, e.expense_total) AS receipt_total,
        SUM(i.line_total) OVER (PARTITION BY e.id) AS item_total,
        ROW_NUMBER() OVER (
            PARTITION BY e.id
            ORDER BY i.line_total DESC, i.id
        ) AS item_number
    FROM budget.expenses e
    JOIN budget.expense_items i ON i.expense_pk = e.id
    WHERE e.source = 'instacart'
),
allocated AS (
    SELECT *,
        ROUND(line_total * receipt_total / NULLIF(item_total, 0), 2)
            AS rounded_amount
    FROM items
),
final_amounts AS (
    SELECT *,
        rounded_amount
        + CASE WHEN item_number = 1 THEN
            receipt_total
            - SUM(rounded_amount) OVER (PARTITION BY receipt_pk)
          ELSE 0
          END AS amount
    FROM allocated
),
report AS (
    SELECT
        receipt_pk,
        0 AS subtotal_row,
        item_pk,
        receipt_id,
        date,
        item,
        qty,
        amount,
        CASE WHEN budget_category = 'Groceries'
             THEN amount ELSE 0 END AS grocery,
        CASE WHEN budget_category = 'Health & Fitness'
             THEN amount ELSE 0 END AS health,
        CASE WHEN budget_category = 'Indoor Supplies'
             THEN amount ELSE 0 END AS indoor,
        CASE WHEN budget_category = 'Outdoor Supplies'
             THEN amount ELSE 0 END AS outdoor,
        CASE WHEN budget_category = 'Cash/Unknown'
             THEN amount ELSE 0 END AS cash,
        CASE WHEN budget_category = 'Clothing'
             THEN amount ELSE 0 END AS clothing,
        CASE WHEN budget_category = 'Medical Products'
             THEN amount ELSE 0 END AS medical
    FROM final_amounts

    UNION ALL

    SELECT
        receipt_pk,
        1,
        NULL,
        receipt_id,
        MAX(date),
        'RECEIPT SUBTOTAL',
        0,
        SUM(amount),
        COALESCE(SUM(amount) FILTER (WHERE budget_category = 'Groceries'), 0),
        COALESCE(SUM(amount) FILTER (WHERE budget_category = 'Health & Fitness'), 0),
        COALESCE(SUM(amount) FILTER (WHERE budget_category = 'Indoor Supplies'), 0),
        COALESCE(SUM(amount) FILTER (WHERE budget_category = 'Outdoor Supplies'), 0),
        COALESCE(SUM(amount) FILTER (WHERE budget_category = 'Cash/Unknown'), 0),
        COALESCE(SUM(amount) FILTER (WHERE budget_category = 'Clothing'), 0),
        COALESCE(SUM(amount) FILTER (WHERE budget_category = 'Medical Products'), 0)
    FROM final_amounts
    GROUP BY receipt_pk, receipt_id
)
SELECT
    receipt_id,
    date,
    item,
    qty,
    amount,
    grocery,
    health,
    indoor,
    outdoor,
    cash,
    clothing,
    medical
FROM report
ORDER BY date DESC, receipt_pk, subtotal_row, item_pk;
