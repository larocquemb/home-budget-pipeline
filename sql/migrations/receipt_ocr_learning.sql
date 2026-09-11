BEGIN;

CREATE TABLE IF NOT EXISTS budget.receipt_ocr_runs (
    run_uuid UUID PRIMARY KEY,
    evidence_id BIGINT NOT NULL REFERENCES budget.receipt_evidence(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    cache_version INTEGER NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    processing_seconds NUMERIC(12, 3),
    worker_host TEXT,
    worker_pid INTEGER,
    merchant TEXT,
    extraction_status TEXT,
    extraction_confidence NUMERIC(5, 4),
    timings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS budget.receipt_ocr_passes (
    run_uuid UUID NOT NULL REFERENCES budget.receipt_ocr_runs(run_uuid) ON DELETE CASCADE,
    pass_id INTEGER NOT NULL CHECK (pass_id > 0),
    page_number INTEGER,
    engine TEXT NOT NULL,
    engine_type TEXT NOT NULL,
    dpi INTEGER,
    psm TEXT,
    variant TEXT NOT NULL,
    seconds NUMERIC(12, 3),
    status TEXT NOT NULL,
    error_type TEXT,
    line_count INTEGER,
    character_count INTEGER,
    structural_score INTEGER,
    summary_score INTEGER,
    valid_timestamp BOOLEAN,
    selected_base BOOLEAN NOT NULL DEFAULT FALSE,
    consensus_line_coverage INTEGER,
    consensus_coverage_ratio NUMERIC(7, 6),
    extracted_text TEXT,
    quality JSONB NOT NULL DEFAULT '{}'::jsonb,
    engine_options JSONB NOT NULL DEFAULT '{}'::jsonb,
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_uuid, pass_id)
);

CREATE TABLE IF NOT EXISTS budget.receipt_ocr_feedback (
    evidence_id BIGINT PRIMARY KEY REFERENCES budget.receipt_evidence(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (outcome IN ('confirmed', 'corrected', 'rejected')),
    corrected_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes TEXT,
    verified_by TEXT,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_receipt_ocr_runs_evidence
    ON budget.receipt_ocr_runs (evidence_id, processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipt_ocr_passes_profile
    ON budget.receipt_ocr_passes (engine, dpi, psm, variant, selected_base);

CREATE OR REPLACE VIEW budget.receipt_ocr_effectiveness AS
SELECT
    COALESCE(r.merchant, '(unknown)') AS merchant,
    COALESCE(lower(substring(r.source_reference from '\.([^.]+)$')), '(none)') AS source_extension,
    p.page_number,
    p.engine,
    p.dpi,
    p.psm,
    p.variant,
    COUNT(*) AS pass_count,
    COUNT(*) FILTER (WHERE p.selected_base) AS selected_count,
    ROUND(AVG(p.seconds), 3) AS average_seconds,
    ROUND(AVG(p.consensus_coverage_ratio), 4) AS average_consensus_coverage,
    ROUND(AVG(p.structural_score), 2) AS average_structural_score,
    COUNT(*) FILTER (WHERE p.selected_base AND f.outcome = 'confirmed') AS selected_confirmed_count,
    COUNT(*) FILTER (WHERE p.selected_base AND f.outcome = 'corrected') AS selected_corrected_count,
    COUNT(*) FILTER (WHERE p.selected_base AND f.outcome = 'rejected') AS selected_rejected_count
FROM budget.receipt_ocr_passes p
JOIN budget.receipt_ocr_runs r USING (run_uuid)
LEFT JOIN budget.receipt_ocr_feedback f ON f.evidence_id = r.evidence_id
GROUP BY COALESCE(r.merchant, '(unknown)'),
         COALESCE(lower(substring(r.source_reference from '\.([^.]+)$')), '(none)'),
         p.page_number, p.engine, p.dpi, p.psm, p.variant;

COMMIT;
