BEGIN;

CREATE SCHEMA IF NOT EXISTS enrichment;

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

COMMIT;
