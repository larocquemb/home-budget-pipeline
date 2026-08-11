WITH items AS (
    SELECT
        e.id AS receipt_pk,
        e.order_id AS receipt_id,
        e.order_date AS date,
        i.id AS item_pk,
        i.item_name AS item,
        COALESCE(i.unit_qty, i.weight_qty) AS qty,
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
             THEN amount END AS grocery_tot,
        CASE WHEN budget_category = 'Health & Fitness'
             THEN amount END AS health_fitness_tot,
        CASE WHEN budget_category = 'Indoor Supplies'
             THEN amount END AS indoor_supplies_tot,
        CASE WHEN budget_category = 'Outdoor Supplies'
             THEN amount END AS outdoor_supplies_tot,
        CASE WHEN budget_category = 'Cash/Unknown'
             THEN amount END AS cash_unknown_tot,
        CASE WHEN budget_category = 'Clothing'
             THEN amount END AS clothing_tot,
        CASE WHEN budget_category = 'Medical Products'
             THEN amount END AS medical_products_tot
    FROM final_amounts

    UNION ALL

    SELECT
        receipt_pk,
        1,
        NULL,
        receipt_id,
        MAX(date),
        'RECEIPT SUBTOTAL',
        NULL,
        SUM(amount),
        SUM(amount) FILTER (WHERE budget_category = 'Groceries'),
        SUM(amount) FILTER (WHERE budget_category = 'Health & Fitness'),
        SUM(amount) FILTER (WHERE budget_category = 'Indoor Supplies'),
        SUM(amount) FILTER (WHERE budget_category = 'Outdoor Supplies'),
        SUM(amount) FILTER (WHERE budget_category = 'Cash/Unknown'),
        SUM(amount) FILTER (WHERE budget_category = 'Clothing'),
        SUM(amount) FILTER (WHERE budget_category = 'Medical Products')
    FROM final_amounts
    GROUP BY receipt_pk, receipt_id
)
SELECT
    receipt_id,
    date,
    item,
    qty,
    amount,
    grocery_tot,
    health_fitness_tot,
    indoor_supplies_tot,
    outdoor_supplies_tot,
    cash_unknown_tot,
    clothing_tot,
    medical_products_tot
FROM report
ORDER BY date DESC, receipt_pk, subtotal_row, item_pk;