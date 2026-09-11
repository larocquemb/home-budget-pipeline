BEGIN;

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
