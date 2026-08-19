-- KAN-76: preserve multiple receipt artifacts for one canonical expense.
-- A scanned paper receipt and an electronic receipt may represent the same
-- financial transaction but contain different evidence (including handwritten
-- categorization clues on paper).

BEGIN;

CREATE TABLE IF NOT EXISTS budget.receipt_evidence (
    id BIGSERIAL PRIMARY KEY,
    expense_pk BIGINT REFERENCES budget.expenses(id) ON DELETE CASCADE,

    evidence_type TEXT NOT NULL CHECK (
        evidence_type IN ('scanned', 'electronic', 'email', 'photo', 'other')
    ),
    source_reference TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    mime_type TEXT,

    transaction_datetime TIMESTAMP,
    merchant TEXT,
    receipt_id TEXT,
    total NUMERIC(12, 2),
    payment_method TEXT,
    card_last4 TEXT,

    extraction_status TEXT,
    extraction_confidence NUMERIC(5, 4),
    raw_text TEXT,
    page_text JSONB,
    raw_payload JSONB,

    is_primary_source BOOLEAN NOT NULL DEFAULT FALSE,
    has_handwritten_notes BOOLEAN NOT NULL DEFAULT FALSE,
    has_category_markup BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_receipt_evidence_sha UNIQUE (source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_receipt_evidence_expense
    ON budget.receipt_evidence (expense_pk);

CREATE INDEX IF NOT EXISTS idx_receipt_evidence_match
    ON budget.receipt_evidence (merchant, transaction_datetime, total, card_last4);

CREATE TABLE IF NOT EXISTS budget.receipt_annotations (
    id BIGSERIAL PRIMARY KEY,
    evidence_id BIGINT NOT NULL REFERENCES budget.receipt_evidence(id) ON DELETE CASCADE,

    annotation_type TEXT NOT NULL CHECK (
        annotation_type IN ('category_label', 'separator', 'circle', 'checkmark', 'note', 'other')
    ),
    page_number INTEGER,
    text TEXT,
    normalized_category TEXT,
    confidence NUMERIC(5, 4),

    -- Coordinates are optional normalized page coordinates (0..1), retained so
    -- visual category boundaries can later be mapped back to receipt items.
    x1 NUMERIC(8, 6),
    y1 NUMERIC(8, 6),
    x2 NUMERIC(8, 6),
    y2 NUMERIC(8, 6),

    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_receipt_annotations_evidence
    ON budget.receipt_annotations (evidence_id, page_number);

CREATE INDEX IF NOT EXISTS idx_receipt_annotations_category
    ON budget.receipt_annotations (normalized_category)
    WHERE normalized_category IS NOT NULL;

COMMIT;
