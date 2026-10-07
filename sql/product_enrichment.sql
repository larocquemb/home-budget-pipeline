BEGIN;

CREATE SCHEMA IF NOT EXISTS enrichment;

CREATE TABLE IF NOT EXISTS enrichment.brave_query_cache (
    cache_key TEXT PRIMARY KEY,
    request JSONB NOT NULL,
    results JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment.product_cache (
    id BIGSERIAL PRIMARY KEY,
    merchant_key TEXT NOT NULL,
    receipt_text_norm TEXT NOT NULL,
    product_description TEXT NOT NULL,
    product_url TEXT NOT NULL,
    provider TEXT NOT NULL,
    search_query TEXT,
    confidence NUMERIC(5,4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'review', 'rejected', 'error')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    use_count INTEGER NOT NULL DEFAULT 1 CHECK (use_count >= 1),
    UNIQUE (merchant_key, receipt_text_norm)
);

CREATE INDEX IF NOT EXISTS idx_product_cache_lookup
    ON enrichment.product_cache (merchant_key, receipt_text_norm, status, confidence DESC);

-- Append-only experiments: never update canonical product fields or accepted cache.
CREATE TABLE IF NOT EXISTS enrichment.product_comparisons (
    id BIGSERIAL PRIMARY KEY,
    run_uuid UUID NOT NULL,
    expense_item_id BIGINT NOT NULL REFERENCES budget.expense_items(id) ON DELETE CASCADE,
    payload JSONB NOT NULL,
    compared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_uuid, expense_item_id)
);
CREATE INDEX IF NOT EXISTS idx_product_comparisons_item
    ON enrichment.product_comparisons (expense_item_id, compared_at DESC);

CREATE TABLE IF NOT EXISTS enrichment.product_comparison_runs (
    run_uuid UUID PRIMARY KEY,
    summary JSONB NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS enrichment.receipt_collaborations (
    id BIGSERIAL PRIMARY KEY,
    run_uuid UUID NOT NULL,
    expense_item_id BIGINT NOT NULL REFERENCES budget.expense_items(id) ON DELETE CASCADE,
    payload JSONB NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_uuid, expense_item_id)
);
CREATE INDEX IF NOT EXISTS idx_receipt_collaborations_item
    ON enrichment.receipt_collaborations (expense_item_id, completed_at DESC);
CREATE TABLE IF NOT EXISTS enrichment.receipt_collaboration_runs (
    run_uuid UUID PRIMARY KEY,
    summary JSONB NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Authoritative lineage survives graph outages and RabbitMQ acknowledgements.
CREATE SCHEMA IF NOT EXISTS lineage;
CREATE TABLE IF NOT EXISTS lineage.receipt_events (
    id UUID PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS receipt_events_source ON lineage.receipt_events(source_sha256, occurred_at);

COMMIT;
