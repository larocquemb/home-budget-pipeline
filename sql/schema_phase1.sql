-- Canonical Phase 1 schema for the home budget pipeline.
--
-- This file is the bootstrap schema for a fresh database. While the project is
-- still pre-production, keep the current canonical model here rather than
-- accumulating story-specific migrations.
--
-- Apply to a fresh database with:
--   psql -d home_budget -f sql/schema_phase1.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS budget;

CREATE OR REPLACE FUNCTION budget.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- Budget categories are domain data, not payment accounts.
CREATE TABLE IF NOT EXISTS budget.expense_categories (
    id BIGSERIAL PRIMARY KEY,
    category_name TEXT NOT NULL UNIQUE,
    notes TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS budget.expenses (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('instacart', 'costco', 'sobeys')),
    order_id TEXT NOT NULL,
    receipt_filename TEXT,
    order_date DATE,
    store_name TEXT,
    order_url TEXT,
    receipt_item_subtotal NUMERIC(12, 2),
    receipt_discount_total NUMERIC(12, 2) NOT NULL DEFAULT 0,
    receipt_tip NUMERIC(12, 2),
    receipt_service_fee NUMERIC(12, 2),
    receipt_recycling_fee NUMERIC(12, 2) NOT NULL DEFAULT 0,
    receipt_service_fee_tax NUMERIC(12, 2) NOT NULL DEFAULT 0,
    receipt_gst NUMERIC(12, 2) NOT NULL DEFAULT 0,
    receipt_pst NUMERIC(12, 2) NOT NULL DEFAULT 0,
    receipt_total_charged NUMERIC(12, 2),
    expense_total NUMERIC(12, 2),
    total_recon_diff NUMERIC(12, 2) GENERATED ALWAYS AS (
        COALESCE(receipt_item_subtotal, 0) + COALESCE(receipt_discount_total, 0)
        + COALESCE(receipt_tip, 0) + COALESCE(receipt_service_fee, 0)
        + COALESCE(receipt_recycling_fee, 0) + COALESCE(receipt_service_fee_tax, 0)
        + COALESCE(receipt_gst, 0) + COALESCE(receipt_pst, 0)
        - COALESCE(expense_total, 0)
    ) STORED,
    raw_page_title TEXT,
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_expenses_source_order_id UNIQUE (source, order_id)
);

CREATE TABLE IF NOT EXISTS budget.expense_items (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT NOT NULL REFERENCES budget.expenses(id) ON DELETE CASCADE,
    item_name TEXT NOT NULL,
    item_name_norm TEXT GENERATED ALWAYS AS (lower(trim(item_name))) STORED,
    tax_code TEXT,
    budget_category TEXT REFERENCES budget.expense_categories(category_name),
    category_source TEXT CHECK (category_source IS NULL OR category_source IN ('rule', 'learned_mapping', 'ai', 'manual', 'unresolved')),
    category_confidence NUMERIC(5, 4) CHECK (category_confidence IS NULL OR (category_confidence >= 0 AND category_confidence <= 1)),
    category_rationale TEXT,
    category_requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    categorized_at TIMESTAMPTZ,
    unit_qty NUMERIC(12, 3),
    unit_cost NUMERIC(12, 2),
    weight_qty NUMERIC(12, 3),
    weight_unit TEXT,
    line_total NUMERIC(12, 2),
    original_line_total NUMERIC(12, 2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS budget.expense_category_mappings (
    id BIGSERIAL PRIMARY KEY,
    source TEXT,
    merchant TEXT,
    match_type TEXT NOT NULL DEFAULT 'contains' CHECK (match_type IN ('exact', 'contains')),
    match_text TEXT NOT NULL,
    category_name TEXT NOT NULL REFERENCES budget.expense_categories(category_name),
    priority INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    provenance TEXT NOT NULL DEFAULT 'rule' CHECK (provenance IN ('rule', 'learned_mapping', 'manual')),
    is_approved BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_expenses_source_date ON budget.expenses (source, order_date DESC);
CREATE INDEX IF NOT EXISTS idx_expenses_store_date ON budget.expenses (store_name, order_date DESC);
CREATE INDEX IF NOT EXISTS idx_expense_items_expense_pk ON budget.expense_items (expense_pk);
CREATE INDEX IF NOT EXISTS idx_expense_items_category ON budget.expense_items (budget_category);
CREATE INDEX IF NOT EXISTS idx_expense_items_review ON budget.expense_items (category_requires_review) WHERE category_requires_review = TRUE;
CREATE INDEX IF NOT EXISTS idx_expense_items_name_norm ON budget.expense_items (item_name_norm);
CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_items_natural ON budget.expense_items (expense_pk, item_name_norm, COALESCE(unit_qty, -1), COALESCE(weight_qty, -1), COALESCE(weight_unit, ''), COALESCE(unit_cost, -1), COALESCE(line_total, -1));
CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_category_mappings_match ON budget.expense_category_mappings (COALESCE(lower(trim(source)), ''), COALESCE(lower(trim(merchant)), ''), match_type, lower(trim(match_text)));
CREATE INDEX IF NOT EXISTS idx_expense_category_mappings_active_priority ON budget.expense_category_mappings (is_active, priority, id);

DROP TRIGGER IF EXISTS trg_expenses_set_updated_at ON budget.expenses;
CREATE TRIGGER trg_expenses_set_updated_at BEFORE UPDATE ON budget.expenses FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
DROP TRIGGER IF EXISTS trg_expense_items_set_updated_at ON budget.expense_items;
CREATE TRIGGER trg_expense_items_set_updated_at BEFORE UPDATE ON budget.expense_items FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
DROP TRIGGER IF EXISTS trg_expense_categories_set_updated_at ON budget.expense_categories;
CREATE TRIGGER trg_expense_categories_set_updated_at BEFORE UPDATE ON budget.expense_categories FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
DROP TRIGGER IF EXISTS trg_expense_category_mappings_set_updated_at ON budget.expense_category_mappings;
CREATE TRIGGER trg_expense_category_mappings_set_updated_at BEFORE UPDATE ON budget.expense_category_mappings FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();

CREATE OR REPLACE VIEW budget.expense_category_splits AS
SELECT expense_pk, budget_category AS category_name,
       ROUND(SUM(COALESCE(line_total, 0)), 2) AS category_amount,
       COUNT(*) AS item_count,
       BOOL_OR(category_requires_review) AS requires_review
FROM budget.expense_items
WHERE budget_category IS NOT NULL
GROUP BY expense_pk, budget_category;

INSERT INTO budget.expense_categories (category_name, notes)
VALUES
    ('Child Support', 'Child support payments'),
    ('Autopac', 'Vehicle and trailer insurance'),
    ('Car Payment', 'Vehicle loan or lease payments'),
    ('Car Repair', 'Vehicle maintenance and repairs, including oil changes'),
    ('Car Replacement Fund', 'Savings toward a future vehicle replacement'),
    ('Real Estate Tax', 'Property taxes'),
    ('Rental Expenses', 'Expenses associated with rental property'),
    ('Cleaning', 'Household cleaning services'),
    ('Clothing', 'Adult clothing and footwear'),
    ('Debt', 'Debt and line-of-credit payments'),
    ('Dining', 'Restaurant, takeout, and dining expenses'),
    ('Misc Paul & Rox', 'General household discretionary expenses not assigned elsewhere'),
    ('Medical ProfSvcs', 'Doctor, dentist, optometrist, physiotherapy, and other professional health services'),
    ('University / Books', 'Post-secondary education, books, and related expenses'),
    ('Emergency Fund', 'Savings reserved for emergencies'),
    ('Fuel', 'Vehicle fuel'),
    ('Fun / Entertainment', 'Entertainment, recreation, events, sports, and leisure activities'),
    ('Furniture / Appliances', 'Furniture and household appliances'),
    ('Birthday / Celebrations', 'Celebrations, hosting, and special-occasion costs'),
    ('Groceries', 'Food and household grocery purchases; excludes celebration-specific food'),
    ('IncomeTax Due', 'Income tax payments'),
    ('Home Insurance', 'Home insurance premiums'),
    ('Mortgage PrePayment', 'Additional mortgage principal payments'),
    ('Interest Expense', 'Interest expense not categorized elsewhere'),
    ('Life Insurance', 'Life insurance premiums'),
    ('Medical Products', 'Prescription drugs, vision products, and over-the-counter medical products'),
    ('LTD Insurance', 'Long-term disability insurance premiums'),
    ('Home Mortgage', 'Regular mortgage payments'),
    ('Lake', NULL),
    ('Sinking Fund', 'Savings reserved for planned future expenses'),
    ('Donations', 'Charitable and community donations'),
    ('Vacation', 'Vacation and personal travel expenses'),
    ('Christmas', 'Christmas-related expenses'),
    ('Home Improvement', 'Home renovations, improvements, and major repairs'),
    ('RRSP GIC', 'RRSP-held guaranteed investment certificates'),
    ('RRSP', 'Registered retirement savings contributions'),
    ('Health & Fitness', 'Fitness memberships, apps, equipment, protein products, vitamins, and wellness programs'),
    ('Accounting', 'Accounting and tax-preparation services'),
    ('Hydro', 'Electric utility charges'),
    ('Online Svcs', 'Streaming and online subscription services'),
    ('Wireless', 'Mobile phone service'),
    ('TV', 'Television and internet services'),
    ('Water', 'Municipal or utility water charges'),
    ('Bank Fee', 'Banking and credit-card fees'),
    ('MLCC', 'Alcohol and liquor-store purchases'),
    ('Cash/Unknown', 'Cash withdrawals and transactions requiring classification'),
    ('Kids Clothing', 'Children''s clothing and footwear'),
    ('Kids Sports', 'Children''s sports and recreation expenses'),
    ('Emp Reimburse', 'Employment-related reimbursements'),
    ('Principal Expense', 'Work-related or business-principal discretionary expenses'),
    ('Student Expense', 'School supplies, field trips, school pictures, and student expenses'),
    ('Hair/Salon/Body Care', 'Hair, salon, spa, cosmetics, and personal-care services'),
    ('Future Use1', NULL),
    ('Indoor Supplies', 'Household cleaning, laundry, hygiene, and consumable indoor supplies'),
    ('Outdoor Supplies', 'Lawn, garden, outdoor maintenance, and seasonal supplies'),
    ('Future Use2', NULL),
    ('Parking', 'Parking fees'),
    ('Shareholder loan', 'Shareholder loan transactions'),
    ('TFSA', 'Tax-Free Savings Account contributions'),
    ('Asset Purchase', 'Purchases capitalized as assets')
ON CONFLICT (category_name)
DO UPDATE SET notes = EXCLUDED.notes;

COMMIT;