-- KAN-76: scanned/paper receipt support for the canonical budget schema.
-- Apply after sql/schema_phase1.sql.

BEGIN;

-- Expand the canonical source vocabulary without creating a parallel receipt table.
ALTER TABLE budget.expenses
    DROP CONSTRAINT IF EXISTS expenses_source_check;

ALTER TABLE budget.expenses
    ADD CONSTRAINT expenses_source_check
    CHECK (source IN ('instacart', 'costco', 'sobeys', 'scanned'));

ALTER TABLE budget.expenses
    ADD COLUMN IF NOT EXISTS source_reference TEXT,
    ADD COLUMN IF NOT EXISTS source_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS extraction_status TEXT NOT NULL DEFAULT 'complete',
    ADD COLUMN IF NOT EXISTS extraction_confidence NUMERIC(5, 4);

ALTER TABLE budget.expenses
    DROP CONSTRAINT IF EXISTS expenses_extraction_status_check;

ALTER TABLE budget.expenses
    ADD CONSTRAINT expenses_extraction_status_check
    CHECK (extraction_status IN ('complete', 'review', 'unreadable'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_expenses_scanned_sha256
    ON budget.expenses (source, source_sha256)
    WHERE source = 'scanned' AND source_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_expenses_extraction_review
    ON budget.expenses (extraction_status, extraction_confidence)
    WHERE extraction_status <> 'complete';

COMMIT;
