-- Disposable receipt-ingestion identity and processing state.

BEGIN;

CREATE SCHEMA IF NOT EXISTS ingest;

CREATE OR REPLACE FUNCTION ingest.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- Stable source identity for rebuildable receipt data. source_sha256 is the
-- cross-schema join key; source_reference is the human-readable relative path.
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

-- Read-only compatibility surface for the existing Ledger UI. The underlying
-- lifecycle state remains disposable in ingest; source_reference is joined by
-- the stable SHA identity rather than duplicated in the status table.
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

COMMIT;
