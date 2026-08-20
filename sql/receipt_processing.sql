-- Receipt backlog processing state for KAN-82.

BEGIN;

CREATE TABLE budget.receipt_processing_status (
    source_sha256 TEXT PRIMARY KEY,
    source_reference TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('processing', 'succeeded', 'review_required', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    first_attempted_at TIMESTAMPTZ,
    last_attempted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_receipt_processing_status_status
    ON budget.receipt_processing_status (status, last_attempted_at DESC);

CREATE TRIGGER trg_receipt_processing_status_set_updated_at
BEFORE UPDATE ON budget.receipt_processing_status
FOR EACH ROW EXECUTE FUNCTION budget.set_updated_at();

COMMIT;
