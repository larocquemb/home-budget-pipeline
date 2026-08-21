-- Canonical bootstrap schema for BrownRook Ledger.
-- Domain configuration such as category names, groups, aliases, and household
-- accounts belongs in configuration/import data rather than in this DDL.

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

-- ---------------------------------------------------------------------------
-- Budget taxonomy
-- ---------------------------------------------------------------------------

CREATE TABLE budget.category_groups (
    id BIGSERIAL PRIMARY KEY,
    group_key TEXT NOT NULL UNIQUE,
    group_name TEXT NOT NULL UNIQUE,
    description TEXT,
    sort_order INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE budget.expense_categories (
    id BIGSERIAL PRIMARY KEY,
    category_name TEXT NOT NULL UNIQUE,
    group_id BIGINT NOT NULL REFERENCES budget.category_groups(id),
    description TEXT,
    sort_order INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE budget.expense_category_aliases (
    id BIGSERIAL PRIMARY KEY,
    alias_name TEXT NOT NULL UNIQUE,
    category_id BIGINT NOT NULL REFERENCES budget.expense_categories(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE budget.expense_category_mappings (
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

CREATE TABLE budget.expense_category_mapping_audit (
    id BIGSERIAL PRIMARY KEY,
    mapping_id BIGINT REFERENCES budget.expense_category_mappings(id) ON DELETE SET NULL,
    expense_item_id BIGINT,
    actor_user TEXT NOT NULL,
    actor_email TEXT,
    action TEXT NOT NULL CHECK (action IN ('created', 'changed', 'disabled')),
    item_name TEXT NOT NULL,
    match_text TEXT NOT NULL,
    source TEXT,
    merchant TEXT,
    old_category TEXT,
    new_category TEXT,
    affected_item_count INTEGER NOT NULL DEFAULT 0 CHECK (affected_item_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Financial accounts and balance history
-- ---------------------------------------------------------------------------

CREATE TABLE budget.accounts (
    id BIGSERIAL PRIMARY KEY,
    account_name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL
        CHECK (account_type IN ('chequing', 'savings', 'investment', 'prepaid', 'credit_card', 'cash', 'other')),
    currency CHAR(3) NOT NULL DEFAULT 'CAD',
    goal_amount NUMERIC(12, 2),
    sort_order INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (goal_amount IS NULL OR goal_amount >= 0)
);

CREATE TABLE budget.account_balances (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES budget.accounts(id) ON DELETE CASCADE,
    balance_date DATE NOT NULL,
    cleared_balance NUMERIC(14, 2),
    current_balance NUMERIC(14, 2) NOT NULL,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_account_balances_day UNIQUE (account_id, balance_date)
);

CREATE TABLE budget.payment_cards (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT REFERENCES budget.accounts(id) ON DELETE SET NULL,
    card_label TEXT NOT NULL,
    card_network TEXT NOT NULL,
    card_last4 TEXT NOT NULL CHECK (card_last4 ~ '^[0-9]{4}$'),
    owner_name TEXT,
    issuer TEXT,
    valid_from DATE NOT NULL,
    valid_to DATE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- ---------------------------------------------------------------------------
-- Canonical expenses and line items
-- ---------------------------------------------------------------------------

CREATE TABLE budget.expenses (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL
        CHECK (source IN ('instacart', 'costco', 'sobeys', 'scanned')),
    order_id TEXT NOT NULL,
    account_id BIGINT REFERENCES budget.accounts(id) ON DELETE SET NULL,
    receipt_filename TEXT,
    source_reference TEXT,
    source_sha256 TEXT,
    order_date DATE,
    transaction_datetime TIMESTAMP,
    store_name TEXT,
    order_url TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'complete'
        CHECK (extraction_status IN ('complete', 'review', 'unreadable')),
    extraction_confidence NUMERIC(5, 4)
        CHECK (extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 1)),
    payment_method TEXT,
    card_last4 TEXT CHECK (card_last4 IS NULL OR card_last4 ~ '^[0-9]{4}$'),
    payer TEXT,
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
        + COALESCE(receipt_tip, 0)
        + COALESCE(receipt_service_fee, 0)
        + COALESCE(receipt_recycling_fee, 0)
        + COALESCE(receipt_service_fee_tax, 0)
        + COALESCE(receipt_gst, 0)
        + COALESCE(receipt_pst, 0)
        - COALESCE(receipt_discount_total, 0)
        - COALESCE(expense_total, 0)
    ) STORED,
    raw_page_title TEXT,
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_expenses_source_order_id UNIQUE (source, order_id)
);

COMMENT ON COLUMN budget.expenses.transaction_datetime IS
    'Receipt-local transaction date/time extracted from source evidence; no timezone is assumed.';

CREATE TABLE budget.expense_items (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT NOT NULL REFERENCES budget.expenses(id) ON DELETE CASCADE,
    item_name TEXT NOT NULL,
    item_name_norm TEXT GENERATED ALWAYS AS (lower(trim(item_name))) STORED,
    tax_code TEXT,
    budget_category TEXT REFERENCES budget.expense_categories(category_name),
    category_source TEXT
        CHECK (category_source IS NULL OR category_source IN ('rule', 'learned_mapping', 'ai', 'manual', 'unresolved')),
    category_confidence NUMERIC(5, 4)
        CHECK (category_confidence IS NULL OR (category_confidence >= 0 AND category_confidence <= 1)),
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

CREATE TABLE budget.expense_reconciliation_results (
    expense_pk BIGINT PRIMARY KEY REFERENCES budget.expenses(id) ON DELETE CASCADE,
    item_subtotal_status TEXT NOT NULL
        CHECK (item_subtotal_status IN ('pass', 'fail', 'not_checkable')),
    item_subtotal_variance NUMERIC(12, 2),
    receipt_total_status TEXT NOT NULL
        CHECK (receipt_total_status IN ('pass', 'fail', 'not_checkable')),
    receipt_total_variance NUMERIC(12, 2),
    category_splits_status TEXT NOT NULL
        CHECK (category_splits_status IN ('pass', 'fail', 'not_checkable')),
    category_splits_variance NUMERIC(12, 2),
    requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE budget.expense_data_quality_results (
    expense_pk BIGINT PRIMARY KEY REFERENCES budget.expenses(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'not_applicable')),
    requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE budget.expense_data_quality_violations (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT NOT NULL REFERENCES budget.expenses(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    field_name TEXT NOT NULL,
    item_id BIGINT REFERENCES budget.expense_items(id) ON DELETE CASCADE,
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Receipt evidence and annotations
-- ---------------------------------------------------------------------------

CREATE TABLE budget.receipt_evidence (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT REFERENCES budget.expenses(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL
        CHECK (evidence_type IN ('scanned', 'electronic', 'email', 'photo', 'other')),
    source_reference TEXT NOT NULL,
    source_sha256 TEXT NOT NULL UNIQUE,
    mime_type TEXT,
    transaction_datetime TIMESTAMP,
    merchant TEXT,
    receipt_id TEXT,
    total NUMERIC(12, 2),
    payment_method TEXT,
    card_last4 TEXT CHECK (card_last4 IS NULL OR card_last4 ~ '^[0-9]{4}$'),
    extraction_status TEXT,
    extraction_confidence NUMERIC(5, 4)
        CHECK (extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 1)),
    raw_text TEXT,
    page_text JSONB,
    raw_payload JSONB,
    is_primary_source BOOLEAN NOT NULL DEFAULT FALSE,
    has_handwritten_notes BOOLEAN NOT NULL DEFAULT FALSE,
    has_category_markup BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE budget.receipt_annotations (
    id BIGSERIAL PRIMARY KEY,
    evidence_id BIGINT NOT NULL REFERENCES budget.receipt_evidence(id) ON DELETE CASCADE,
    annotation_type TEXT NOT NULL
        CHECK (annotation_type IN ('category_label', 'category_subtotal', 'separator', 'circle', 'checkmark', 'note', 'other')),
    page_number INTEGER,
    text TEXT,
    normalized_category TEXT,
    amount NUMERIC(12, 2),
    line_index INTEGER,
    confidence NUMERIC(5, 4)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    x1 NUMERIC(8, 6),
    y1 NUMERIC(8, 6),
    x2 NUMERIC(8, 6),
    y2 NUMERIC(8, 6),
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX idx_expense_categories_group ON budget.expense_categories (group_id, sort_order);
CREATE UNIQUE INDEX uq_expense_category_mappings_match
    ON budget.expense_category_mappings (
        COALESCE(lower(trim(source)), ''),
        COALESCE(lower(trim(merchant)), ''),
        match_type,
        lower(trim(match_text))
    );
CREATE INDEX idx_expense_category_mappings_active_priority
    ON budget.expense_category_mappings (is_active, priority, id);
CREATE INDEX idx_expense_category_mapping_audit_mapping
    ON budget.expense_category_mapping_audit (mapping_id, created_at DESC);
CREATE INDEX idx_expense_category_mapping_audit_item
    ON budget.expense_category_mapping_audit (expense_item_id, created_at DESC);
CREATE INDEX idx_account_balances_latest
    ON budget.account_balances (account_id, balance_date DESC);
CREATE INDEX idx_payment_cards_lookup
    ON budget.payment_cards (lower(card_network), card_last4, valid_from, valid_to);
CREATE UNIQUE INDEX uq_payment_cards_version
    ON budget.payment_cards (lower(card_network), card_last4, valid_from, COALESCE(owner_name, ''));
CREATE INDEX idx_expenses_source_date ON budget.expenses (source, order_date DESC);
CREATE INDEX idx_expenses_store_date ON budget.expenses (store_name, order_date DESC);
CREATE INDEX idx_expenses_store_datetime ON budget.expenses (store_name, transaction_datetime DESC);
CREATE UNIQUE INDEX uq_expenses_scanned_sha256
    ON budget.expenses (source, source_sha256)
    WHERE source = 'scanned' AND source_sha256 IS NOT NULL;
CREATE INDEX idx_expenses_extraction_review
    ON budget.expenses (extraction_status, extraction_confidence)
    WHERE extraction_status <> 'complete';
CREATE INDEX idx_expenses_payment_card
    ON budget.expenses (payment_method, card_last4, order_date)
    WHERE card_last4 IS NOT NULL;
CREATE INDEX idx_expense_items_expense_pk ON budget.expense_items (expense_pk);
CREATE INDEX idx_expense_items_category ON budget.expense_items (budget_category);
CREATE INDEX idx_expense_items_review
    ON budget.expense_items (category_requires_review)
    WHERE category_requires_review = TRUE;
CREATE INDEX idx_expense_items_name_norm ON budget.expense_items (item_name_norm);
CREATE UNIQUE INDEX uq_expense_items_natural
    ON budget.expense_items (
        expense_pk,
        item_name_norm,
        COALESCE(unit_qty, -1),
        COALESCE(weight_qty, -1),
        COALESCE(weight_unit, ''),
        COALESCE(unit_cost, -1),
        COALESCE(line_total, -1)
    );
CREATE INDEX idx_expense_reconciliation_review
    ON budget.expense_reconciliation_results (requires_review)
    WHERE requires_review = TRUE;
CREATE INDEX idx_expense_data_quality_review
    ON budget.expense_data_quality_results (requires_review)
    WHERE requires_review = TRUE;
CREATE INDEX idx_expense_data_quality_violations_expense
    ON budget.expense_data_quality_violations (expense_pk, rule);
CREATE INDEX idx_receipt_evidence_expense ON budget.receipt_evidence (expense_pk);
CREATE INDEX idx_receipt_evidence_match
    ON budget.receipt_evidence (merchant, transaction_datetime, total, card_last4);
CREATE INDEX idx_receipt_annotations_evidence
    ON budget.receipt_annotations (evidence_id, page_number);
CREATE INDEX idx_receipt_annotations_category
    ON budget.receipt_annotations (normalized_category)
    WHERE normalized_category IS NOT NULL;

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------

CREATE TRIGGER trg_category_groups_set_updated_at BEFORE UPDATE ON budget.category_groups
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_expense_categories_set_updated_at BEFORE UPDATE ON budget.expense_categories
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_expense_category_mappings_set_updated_at BEFORE UPDATE ON budget.expense_category_mappings
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_accounts_set_updated_at BEFORE UPDATE ON budget.accounts
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_payment_cards_set_updated_at BEFORE UPDATE ON budget.payment_cards
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_expenses_set_updated_at BEFORE UPDATE ON budget.expenses
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_expense_items_set_updated_at BEFORE UPDATE ON budget.expense_items
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_receipt_evidence_set_updated_at BEFORE UPDATE ON budget.receipt_evidence
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();
CREATE TRIGGER trg_receipt_annotations_set_updated_at BEFORE UPDATE ON budget.receipt_annotations
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();

-- ---------------------------------------------------------------------------
-- Reporting views
-- ---------------------------------------------------------------------------

CREATE VIEW budget.expense_category_splits AS
SELECT
    ei.expense_pk,
    ec.category_name,
    cg.group_key,
    cg.group_name,
    ROUND(SUM(COALESCE(ei.line_total, 0)), 2) AS category_amount,
    COUNT(*) AS item_count,
    BOOL_OR(ei.category_requires_review) AS requires_review
FROM budget.expense_items ei
JOIN budget.expense_categories ec ON ec.category_name = ei.budget_category
JOIN budget.category_groups cg ON cg.id = ec.group_id
GROUP BY ei.expense_pk, ec.category_name, cg.group_key, cg.group_name;

CREATE VIEW budget.expense_reconciliation_status AS
SELECT
    e.id AS expense_pk,
    e.source,
    e.order_id,
    e.order_date,
    e.store_name,
    r.item_subtotal_status,
    r.item_subtotal_variance,
    r.receipt_total_status,
    r.receipt_total_variance,
    r.category_splits_status,
    r.category_splits_variance,
    r.requires_review,
    r.reconciled_at
FROM budget.expenses e
LEFT JOIN budget.expense_reconciliation_results r ON r.expense_pk = e.id;

CREATE VIEW budget.expense_data_quality_status AS
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

CREATE VIEW budget.account_current_balances AS
SELECT DISTINCT ON (a.id)
    a.id AS account_id,
    a.account_name,
    a.account_type,
    a.currency,
    a.goal_amount,
    b.balance_date,
    b.cleared_balance,
    b.current_balance,
    CASE
        WHEN a.goal_amount IS NULL OR a.goal_amount = 0 OR b.cleared_balance IS NULL THEN NULL
        ELSE ROUND((b.cleared_balance / a.goal_amount) * 100, 1)
    END AS goal_percent
FROM budget.accounts a
LEFT JOIN budget.account_balances b ON b.account_id = a.id
WHERE a.is_active = TRUE
ORDER BY a.id, b.balance_date DESC NULLS LAST, b.id DESC NULLS LAST;

COMMIT;
