-- Phase 1 canonical schema for budget ingestion (Costco, Instacart, Sobeys)
-- Apply:
--   psql -d postgres -f sql/schema_phase1.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS budget;

CREATE TABLE IF NOT EXISTS budget.expenses (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('instacart', 'costco', 'sobeys')),
    order_id TEXT NOT NULL,
    receipt_filename TEXT,
    order_date DATE,
    store_name TEXT,
    order_url TEXT,

    -- Receipt-level amounts
    receipt_item_subtotal NUMERIC(12, 2),
    receipt_discount_total NUMERIC(12, 2),
    receipt_tip NUMERIC(12, 2),
    receipt_service_fee NUMERIC(12, 2),
    receipt_recycling_fee NUMERIC(12, 2),
    receipt_service_fee_tax NUMERIC(12, 2),
    receipt_gst NUMERIC(12, 2),
    receipt_pst NUMERIC(12, 2),
    receipt_total_charged NUMERIC(12, 2),

    expense_total NUMERIC(12, 2),

    raw_page_title TEXT,
    raw_payload JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Natural key to support idempotent upsert by source + retailer expense id
    CONSTRAINT uq_expenses_source_order_id UNIQUE (source, order_id)
);

ALTER TABLE budget.expenses
    ADD COLUMN IF NOT EXISTS receipt_filename TEXT;

ALTER TABLE budget.expenses
    DROP COLUMN IF EXISTS costco_order_id;

CREATE TABLE IF NOT EXISTS budget.expense_items (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT NOT NULL REFERENCES budget.expenses(id) ON DELETE CASCADE,

    item_name TEXT NOT NULL,
    item_name_norm TEXT GENERATED ALWAYS AS (lower(trim(item_name))) STORED,

    budget_category TEXT,
    category_source TEXT,

    -- Count-based items
    unit_qty NUMERIC(12, 3),
    unit_cost NUMERIC(12, 2),

    -- Weighted items
    weight_qty NUMERIC(12, 3),
    weight_unit TEXT,

    line_total NUMERIC(12, 2),
    original_line_total NUMERIC(12, 2),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_expenses_source_date
    ON budget.expenses (source, order_date DESC);

CREATE INDEX IF NOT EXISTS idx_expenses_store_date
    ON budget.expenses (store_name, order_date DESC);

CREATE INDEX IF NOT EXISTS idx_expense_items_expense_pk
    ON budget.expense_items (expense_pk);

CREATE INDEX IF NOT EXISTS idx_expense_items_category
    ON budget.expense_items (budget_category);

CREATE INDEX IF NOT EXISTS idx_expense_items_name_norm
    ON budget.expense_items (item_name_norm);

-- Natural key for idempotent upsert at item level.
-- Use a UNIQUE INDEX (not UNIQUE CONSTRAINT) because expression keys are needed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_items_natural
    ON budget.expense_items (
        expense_pk,
        item_name_norm,
        COALESCE(unit_qty, -1),
        COALESCE(weight_qty, -1),
        COALESCE(weight_unit, ''),
        COALESCE(unit_cost, -1),
        COALESCE(line_total, -1)
    );

CREATE OR REPLACE FUNCTION budget.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_expenses_set_updated_at ON budget.expenses;
CREATE TRIGGER trg_expenses_set_updated_at
BEFORE UPDATE ON budget.expenses
FOR EACH ROW
EXECUTE FUNCTION budget.set_updated_at();

DROP TRIGGER IF EXISTS trg_expense_items_set_updated_at ON budget.expense_items;
CREATE TRIGGER trg_expense_items_set_updated_at
BEFORE UPDATE ON budget.expense_items
FOR EACH ROW
EXECUTE FUNCTION budget.set_updated_at();

-- Keep a single canonical model: expenses + expense_items.
DROP TABLE IF EXISTS budget.receipts;

CREATE TABLE IF NOT EXISTS budget.expense_categories (
    id BIGSERIAL PRIMARY KEY,
    category_name TEXT NOT NULL UNIQUE,
    notes TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_expense_categories_set_updated_at ON budget.expense_categories;
CREATE TRIGGER trg_expense_categories_set_updated_at
BEFORE UPDATE ON budget.expense_categories
FOR EACH ROW
EXECUTE FUNCTION budget.set_updated_at();

INSERT INTO budget.expense_categories (category_name, notes)
VALUES
    ('Child Support', 'Combined'),
    ('Autopac', 'CRV, F-150, GM, trailers. No motorcycle or skidoo'),
    ('Car Payment', NULL),
    ('Car Repair', 'includes oil changes, rocker panels'),
    ('Car Replacement Fund', 'Will start saving for once Child Support is done'),
    ('Real Estate Tax', 'Home and Rental'),
    ('Rental Expenses', 'Furnace inspection, sewer'),
    ('Cleaning', 'Cleaning Bee'),
    ('Clothing', 'Charge to House VISA'),
    ('Debt', 'Line of Credit'),
    ('Dining', 'Was $9500 last year'),
    ('Misc Paul & Rox', 'If overbudget in month this will be reduced so we don''t accumulate debt'),
    ('Medical ProfSvcs', 'Doctor, Dentist, Optometrist, Physio'),
    ('University / Books', 'Funded by Business use of Home tax credit for Tetreault post-secondary only if no break in school'),
    ('Emergency Fund', 'Savings, CIBC eAdvantage short term account for transfers'),
    ('Fuel', 'Paul gas $0 paid by Corp, Mylene pays gas after grad'),
    ('Fun / Entertainment', 'Concerts, Comedy Clubs, Festival, Movies, Jets, Bombers, Folklorama, Recreation, Golf'),
    ('Furniture / Appliances', NULL),
    ('Birthday / Celebrations', 'Birthdays, sing alongs, thanksgiving, grad, easter, BDC. Gift/hosting costs'),
    ('Groceries', 'Does not include food for celebrations'),
    ('IncomeTax Due', 'Ensure we buy RRSP so we don''t pay'),
    ('Home Insurance', 'Wawaneesa'),
    ('Mortgage PrePayment', NULL),
    ('Interest Expense', NULL),
    ('Life Insurance', '$1M Paul'),
    ('Medical Products', 'Prescriptions BlueCross net cost, contacts, prescription glasses, over counter drugs'),
    ('LTD Insurance', 'Disability insurance Paul'),
    ('Home Mortgage', NULL),
    ('Lake', 'Plan to sell after Comcast'),
    ('Sinking Fund', 'Caisse 3% Savings Plus account'),
    ('Donations', 'Paroisse'),
    ('Vacation', 'Rox Girls vacation, Ottawa'),
    ('Christmas', NULL),
    ('Home Improvement', 'Home renovations'),
    ('RRSP GIC', NULL),
    ('RRSP', 'Savings, growth for retirement, refund mortgage prepayment'),
    ('Health & Fitness', 'gym, fitness apps, equipment, protein, Weight Watchers, vitamins'),
    ('Accounting', 'tax filing'),
    ('Hydro', NULL),
    ('Online Svcs', 'Streaming services Netflix, Spotify, Corp pays Amazon, Disney'),
    ('Wireless', 'Rox iPhone'),
    ('TV', 'Corp Pays TV and Internet'),
    ('Water', 'RM water'),
    ('Bank Fee', 'VISA annual fee'),
    ('MLCC', 'MLCC, SOBR'),
    ('Cash/Unknown', NULL),
    ('Kids Clothing', 'Charge to House VISA'),
    ('Kids Sports', 'Charge to House VISA, House cheques'),
    ('Emp Reimburse', NULL),
    ('Principal Expense', 'Staff parties, gift, eatiing out'),
    ('Student Expense', 'School supplies, field trips, pictures'),
    ('Hair/Salon/Body Care', 'hair coloring, nails, spa, massages, makeup, leg laser'),
    ('Future Use1', NULL),
    ('Indoor Supplies', 'cleaning, laundry, toothpaste, tampons'),
    ('Outdoor Supplies', 'Chemicals, fertiziler, garden flowers, shovels, hoses'),
    ('Future Use2', 'Automotive, tools stuff for shop work.'),
    ('Parking', NULL),
    ('Shareholder loan', 'Corp loan'),
    ('TFSA', 'Savings, Tax refund to be applied to mortgage in 2024'),
    ('Asset Purchase', NULL)
ON CONFLICT (category_name)
DO UPDATE SET
    notes = EXCLUDED.notes;

COMMIT;
