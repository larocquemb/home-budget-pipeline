-- Disposable receipt-ingestion identity and processing state.
-- This state is derived and intentionally rebuilt instead of migrated.

BEGIN;

DO $$
DECLARE
    relation_kind "char";
BEGIN
    SELECT c.relkind
      INTO relation_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'budget'
       AND c.relname = 'receipt_processing_status';

    IF relation_kind = 'v' THEN
        EXECUTE 'DROP VIEW budget.receipt_processing_status CASCADE';
    ELSIF relation_kind IS NOT NULL THEN
        EXECUTE 'DROP TABLE budget.receipt_processing_status CASCADE';
    END IF;
END;
$$;

DROP SCHEMA IF EXISTS ingest CASCADE;
CREATE SCHEMA ingest;

CREATE OR REPLACE FUNCTION ingest.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE TABLE ingest.receipts (
    source_sha256 TEXT PRIMARY KEY,
    source_reference TEXT NOT NULL,
    first_discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ingest_receipts_source_reference
    ON ingest.receipts (source_reference);

CREATE TRIGGER trg_ingest_receipts_set_updated_at
BEFORE UPDATE ON ingest.receipts
FOR EACH ROW EXECUTE FUNCTION ingest.set_updated_at();

CREATE TABLE ingest.receipt_processing_status (
    source_sha256 TEXT PRIMARY KEY
        REFERENCES ingest.receipts(source_sha256) ON DELETE CASCADE,
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
    ON ingest.receipt_processing_status (status, last_attempted_at DESC);

CREATE TRIGGER trg_receipt_processing_status_set_updated_at
BEFORE UPDATE ON ingest.receipt_processing_status
FOR EACH ROW EXECUTE FUNCTION ingest.set_updated_at();

CREATE VIEW budget.receipt_processing_status AS
SELECT
    s.source_sha256,
    r.source_reference,
    s.status,
    s.attempts,
    s.last_error,
    s.first_attempted_at,
    s.last_attempted_at,
    s.completed_at,
    s.updated_at
FROM ingest.receipt_processing_status s
JOIN ingest.receipts r USING (source_sha256);

CREATE TABLE IF NOT EXISTS budget.receipt_ocr_lines (
    id BIGSERIAL PRIMARY KEY,
    evidence_id BIGINT NOT NULL REFERENCES budget.receipt_evidence(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    text TEXT NOT NULL,
    confidence NUMERIC(5, 4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    x INTEGER, y INTEGER, width INTEGER, height INTEGER,
    page_left INTEGER, page_width INTEGER,
    indent_pixels INTEGER,
    indent_ratio NUMERIC(7, 6),
    indent_level INTEGER,
    line_type TEXT NOT NULL,
    department TEXT,
    applies_to_page INTEGER,
    applies_to_line INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (evidence_id, page_number, line_number)
);

CREATE INDEX IF NOT EXISTS idx_receipt_ocr_lines_context
    ON budget.receipt_ocr_lines (evidence_id, department, line_type);

COMMIT;
