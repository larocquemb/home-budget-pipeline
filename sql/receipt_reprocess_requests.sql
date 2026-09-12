-- Additive state for request completion across redelivery and ingest rebuilds.
CREATE TABLE IF NOT EXISTS budget.receipt_reprocess_requests (
    source_sha256 TEXT NOT NULL,
    request_id UUID NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (source_sha256, request_id)
);
