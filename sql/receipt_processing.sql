-- Physical schema template for disposable receipt-ingestion state.
-- __INGEST_SCHEMA__ is replaced by the blue/green upgrader, e.g. ingest_v1.

CREATE SCHEMA "__INGEST_SCHEMA__";

CREATE OR REPLACE FUNCTION "__INGEST_SCHEMA__".set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE TABLE "__INGEST_SCHEMA__".receipts (
    source_sha256 TEXT PRIMARY KEY,
    source_reference TEXT NOT NULL,
    first_discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ingest_receipts_source_reference
    ON "__INGEST_SCHEMA__".receipts (source_reference);

CREATE TRIGGER trg_ingest_receipts_set_updated_at
BEFORE UPDATE ON "__INGEST_SCHEMA__".receipts
FOR EACH ROW EXECUTE FUNCTION "__INGEST_SCHEMA__".set_updated_at();

CREATE TABLE "__INGEST_SCHEMA__".receipt_processing_status (
    source_sha256 TEXT PRIMARY KEY
        REFERENCES "__INGEST_SCHEMA__".receipts(source_sha256) ON DELETE CASCADE,
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
    ON "__INGEST_SCHEMA__".receipt_processing_status (status, last_attempted_at DESC);

CREATE TRIGGER trg_receipt_processing_status_set_updated_at
BEFORE UPDATE ON "__INGEST_SCHEMA__".receipt_processing_status
FOR EACH ROW EXECUTE FUNCTION "__INGEST_SCHEMA__".set_updated_at();
