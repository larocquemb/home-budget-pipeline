BEGIN;

CREATE TABLE IF NOT EXISTS budget.product_enrichment_cache (
    merchant_key TEXT NOT NULL,
    item_name_norm TEXT NOT NULL,
    provider TEXT NOT NULL,
    search_query TEXT NOT NULL,
    candidate_title TEXT NOT NULL,
    candidate_url TEXT NOT NULL,
    confidence NUMERIC(5,4) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'review', 'rejected', 'error')),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (merchant_key, item_name_norm)
);

CREATE INDEX IF NOT EXISTS idx_product_enrichment_cache_verified
    ON budget.product_enrichment_cache (merchant_key, item_name_norm, status, confidence DESC);

COMMIT;
