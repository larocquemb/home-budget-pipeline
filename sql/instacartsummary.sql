WITH items AS (
    SELECT
        e.id AS receipt_pk,
        e.order_id AS receipt_id,
        e.order_date AS date,
        i.id AS item_pk,
        i.budget_category,
        i.line_total,
        COALESCE(e.receipt_total_charged, e.expense_total) AS receipt_total,
        SUM(i.line_total) OVER (
            PARTITION BY e.id
        ) AS item_total,
        ROW_NUMBER() OVER (
            PARTITION BY e.id
            ORDER BY i.line_total DESC, i.id
        ) AS item_number
    FROM budget.expenses e
    JOIN budget.expense_items i
        ON i.expense_pk = e.id
    WHERE e.source = 'instacart'
),
allocated AS (
    SELECT
        *,
        ROUND(
            line_total * receipt_total
            / NULLIF(item_total, 0),
            2
        ) AS rounded_amount
    FROM items
),
final_amounts AS (
    SELECT
        *,
        rounded_amount
        + CASE
            WHEN item_number = 1 THEN
                receipt_total
                - SUM(rounded_amount) OVER (
                    PARTITION BY receipt_pk
                )
            ELSE 0
          END AS amount
    FROM allocated
)
SELECT
    receipt_id,
    MAX(date) AS date,
    SUM(amount) AS receipt_tot,

    COALESCE(SUM(amount) FILTER (
        WHERE budget_category = 'Groceries'
    ), 0) AS grocery_tot,

    COALESCE(SUM(amount) FILTER (
        WHERE budget_category = 'Health & Fitness'
    ), 0) AS health_tot,

    COALESCE(SUM(amount) FILTER (
        WHERE budget_category = 'Indoor Supplies'
    ), 0) AS indoor_tot,

    COALESCE(SUM(amount) FILTER (
        WHERE budget_category = 'Outdoor Supplies'
    ), 0) AS outdoor_tot,

    COALESCE(SUM(amount) FILTER (
        WHERE budget_category = 'Cash/Unknown'
    ), 0) AS cash_tot,

    COALESCE(SUM(amount) FILTER (
        WHERE budget_category = 'Clothing'
    ), 0) AS clothing_tot,

    COALESCE(SUM(amount) FILTER (
        WHERE budget_category = 'Medical Products'
    ), 0) AS medical_tot

FROM final_amounts
GROUP BY receipt_pk, receipt_id
ORDER BY receipt_id DESC;
