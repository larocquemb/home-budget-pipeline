BEGIN;

ALTER TABLE budget.receipt_ocr_runs
    ADD COLUMN IF NOT EXISTS result_schema_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS traceparent TEXT,
    ADD COLUMN IF NOT EXISTS worker_identity JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS budget.receipt_ocr_artifacts (
    id BIGSERIAL PRIMARY KEY,
    run_uuid UUID NOT NULL REFERENCES budget.receipt_ocr_runs(run_uuid) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    media_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_uuid, uri)
);

CREATE TABLE IF NOT EXISTS budget.ocr_result_events (
    message_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN ('ocr.run-completed.v1', 'ocr.pass-completed.v1')
    ),
    run_uuid UUID NOT NULL REFERENCES budget.receipt_ocr_runs(run_uuid) ON DELETE CASCADE,
    pass_id INTEGER CHECK (pass_id IS NULL OR pass_id > 0),
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_reference TEXT NOT NULL,
    worker_identity JSONB NOT NULL,
    trace_context JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (event_type = 'ocr.run-completed.v1' AND pass_id IS NULL)
        OR (event_type = 'ocr.pass-completed.v1' AND pass_id IS NOT NULL)
    ),
    UNIQUE (run_uuid, pass_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_receipt_ocr_artifacts_sha256
    ON budget.receipt_ocr_artifacts (sha256);

CREATE INDEX IF NOT EXISTS idx_ocr_result_events_source
    ON budget.ocr_result_events (source_sha256, persisted_at DESC);

COMMIT;
