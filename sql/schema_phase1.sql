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

-- Vertex budget categories are domain data, not payment accounts.
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

    -- Receipt-level amounts.
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
        COALESCE(receipt_item_subtotal, 0)
        + COALESCE(receipt_discount_total, 0)
        + COALESCE(receipt_tip, 0)
        + COALESCE(receipt_service_fee, 0)
        + COALESCE(receipt_recycling_fee, 0)
        + COALESCE(receipt_service_fee_tax, 0)
        + COALESCE(receipt_gst, 0)
        + COALESCE(receipt_pst, 0)
        - COALESCE(expense_total, 0)
    ) STORED,

    raw_page_title TEXT,
    raw_payload JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Idempotent source-level ingestion. KAN-77 will link duplicate source
    -- receipts to one canonical purchase rather than duplicating categorization.
    CONSTRAINT uq_expenses_source_order_id UNIQUE (source, order_id)
);

CREATE TABLE IF NOT EXISTS budget.expense_items (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT NOT NULL REFERENCES budget.expenses(id) ON DELETE CASCADE,

    item_name TEXT NOT NULL,
    item_name_norm TEXT GENERATED ALWAYS AS (lower(trim(item_name))) STORED,
    tax_code TEXT,

    -- KAN-68 category decision and audit trail.
    budget_category TEXT REFERENCES budget.expense_categories(category_name),
    category_source TEXT CHECK (
        category_source IS NULL OR category_source IN (
            'rule', 'learned_mapping', 'ai', 'manual', 'unresolved'
        )
    ),
    category_confidence NUMERIC(5, 4) CHECK (
        category_confidence IS NULL
        OR (category_confidence >= 0 AND category_confidence <= 1)
    ),
    category_rationale TEXT,
    category_requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    categorized_at TIMESTAMPTZ,

    -- Count-based items.
    unit_qty NUMERIC(12, 3),
    unit_cost NUMERIC(12, 2),

    -- Weighted items.
    weight_qty NUMERIC(12, 3),
    weight_unit TEXT,

    line_total NUMERIC(12, 2),
    original_line_total NUMERIC(12, 2),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reusable deterministic / learned mappings. AI decisions belong on individual
-- expense_items; only approved corrections become reusable mappings.
CREATE TABLE IF NOT EXISTS budget.expense_category_mappings (
    id BIGSERIAL PRIMARY KEY,
    source TEXT,
    merchant TEXT,
    match_type TEXT NOT NULL DEFAULT 'contains'
        CHECK (match_type IN ('exact', 'contains')),
    match_text TEXT NOT NULL,
    category_name TEXT NOT NULL REFERENCES budget.expense_categories(category_name),
    priority INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    provenance TEXT NOT NULL DEFAULT 'rule'
        CHECK (provenance IN ('rule', 'learned_mapping', 'manual')),
    is_approved BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
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

CREATE INDEX IF NOT EXISTS idx_expense_items_review
    ON budget.expense_items (category_requires_review)
    WHERE category_requires_review = TRUE;

CREATE INDEX IF NOT EXISTS idx_expense_items_name_norm
    ON budget.expense_items (item_name_norm);

-- Natural key for idempotent line-item ingestion.
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_category_mappings_match
    ON budget.expense_category_mappings (
        COALESCE(lower(trim(source)), ''),
        COALESCE(lower(trim(merchant)), ''),
        match_type,
        lower(trim(match_text))
    );

CREATE INDEX IF NOT EXISTS idx_expense_category_mappings_active_priority
    ON budget.expense_category_mappings (is_active, priority, id);

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

DROP TRIGGER IF EXISTS trg_expense_categories_set_updated_at ON budget.expense_categories;
CREATE TRIGGER trg_expense_categories_set_updated_at
BEFORE UPDATE ON budget.expense_categories
FOR EACH ROW
EXECUTE FUNCTION budget.set_updated_at();

DROP TRIGGER IF EXISTS trg_expense_category_mappings_set_updated_at
    ON budget.expense_category_mappings;
CREATE TRIGGER trg_expense_category_mappings_set_updated_at
BEFORE UPDATE ON budget.expense_category_mappings
FOR EACH ROW
EXECUTE FUNCTION budget.set_updated_at();

-- Category splits for a canonical expense. A split remains marked for review if
-- any contributing line item has not yet been approved/finalized.
CREATE OR REPLACE VIEW budget.expense_category_splits AS
SELECT
    expense_pk,
    budget_category AS category_name,
    ROUND(SUM(COALESCE(line_total, 0)), 2) AS category_amount,
    COUNT(*) AS item_count,
    BOOL_OR(category_requires_review) AS requires_review
FROM budget.expense_items
WHERE budget_category IS NOT NULL
GROUP BY expense_pk, budget_category;

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
    ('Lake', NULL),
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