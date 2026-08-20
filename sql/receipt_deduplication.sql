-- KAN-77: auditable cross-source receipt duplicate relationships.

BEGIN;

CREATE TABLE budget.receipt_duplicate_links (
    id BIGSERIAL PRIMARY KEY,
    left_evidence_id BIGINT NOT NULL REFERENCES budget.receipt_evidence(id) ON DELETE CASCADE,
    right_evidence_id BIGINT NOT NULL REFERENCES budget.receipt_evidence(id) ON DELETE CASCADE,
    canonical_expense_pk BIGINT REFERENCES budget.expenses(id) ON DELETE SET NULL,
    score INTEGER NOT NULL CHECK (score >= 0),
    disposition TEXT NOT NULL
        CHECK (disposition IN ('exact_duplicate', 'probable_duplicate', 'review', 'distinct')),
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    item_similarity NUMERIC(5, 4) NOT NULL DEFAULT 0
        CHECK (item_similarity >= 0 AND item_similarity <= 1),
    resolution_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (resolution_status IN ('pending', 'auto_linked', 'confirmed', 'rejected')),
    resolution_source TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (left_evidence_id < right_evidence_id),
    CONSTRAINT uq_receipt_duplicate_pair UNIQUE (left_evidence_id, right_evidence_id)
);

CREATE INDEX idx_receipt_duplicate_links_canonical
    ON budget.receipt_duplicate_links (canonical_expense_pk)
    WHERE canonical_expense_pk IS NOT NULL;
CREATE INDEX idx_receipt_duplicate_links_review
    ON budget.receipt_duplicate_links (resolution_status, disposition, score DESC)
    WHERE resolution_status = 'pending';

CREATE TABLE budget.canonical_item_source_resolutions (
    expense_pk BIGINT PRIMARY KEY REFERENCES budget.expenses(id) ON DELETE CASCADE,
    evidence_id BIGINT NOT NULL REFERENCES budget.receipt_evidence(id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;
