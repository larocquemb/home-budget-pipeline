BEGIN;

INSERT INTO budget.merchant_aliases (alias_name, merchant_name)
VALUES
    ('michaels', 'Michaels'),
    ('michfiels', 'Michaels')
ON CONFLICT (alias_name) DO UPDATE SET
    merchant_name = EXCLUDED.merchant_name,
    is_active = TRUE,
    updated_at = NOW();

COMMIT;
