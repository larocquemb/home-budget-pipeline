-- Phase 1 canonical schema for grocery ingestion (Costco, Instacart, Sobeys)
-- Apply:
--   psql -d postgres -f sql/schema_phase1.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS grocery;

CREATE TABLE IF NOT EXISTS grocery.orders (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('instacart', 'costco', 'sobeys')),
    order_id TEXT NOT NULL,
    order_date DATE,
    store_name TEXT,
    order_url TEXT,

    -- Receipt-level amounts
    receipt_item_subtotal NUMERIC(12, 2),
    receipt_discount_total NUMERIC(12, 2),
    receipt_tip NUMERIC(12, 2),
    receipt_service_fee NUMERIC(12, 2),
    receipt_recycling_fee NUMERIC(12, 2),
    receipt_service_fee_tax NUMERIC(12, 2),
    receipt_gst NUMERIC(12, 2),
    receipt_pst NUMERIC(12, 2),
    receipt_total_charged NUMERIC(12, 2),

    -- Kept for compatibility with current script output
    order_total NUMERIC(12, 2),

    raw_page_title TEXT,
    raw_payload JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Natural key to support idempotent upsert by source + retailer order id
    CONSTRAINT uq_orders_source_order_id UNIQUE (source, order_id)
);

CREATE TABLE IF NOT EXISTS grocery.order_items (
    id BIGSERIAL PRIMARY KEY,
    order_pk BIGINT NOT NULL REFERENCES grocery.orders(id) ON DELETE CASCADE,

    item_name TEXT NOT NULL,
    item_name_norm TEXT GENERATED ALWAYS AS (lower(trim(item_name))) STORED,

    budget_category TEXT,
    category_source TEXT,

    -- Count-based items
    unit_qty NUMERIC(12, 3),
    unit_cost NUMERIC(12, 2),

    -- Weighted items
    weight_qty NUMERIC(12, 3),
    weight_unit TEXT,

    line_total NUMERIC(12, 2),
    original_line_total NUMERIC(12, 2),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_source_date
    ON grocery.orders (source, order_date DESC);

CREATE INDEX IF NOT EXISTS idx_orders_store_date
    ON grocery.orders (store_name, order_date DESC);

CREATE INDEX IF NOT EXISTS idx_order_items_order_pk
    ON grocery.order_items (order_pk);

CREATE INDEX IF NOT EXISTS idx_order_items_category
    ON grocery.order_items (budget_category);

CREATE INDEX IF NOT EXISTS idx_order_items_name_norm
    ON grocery.order_items (item_name_norm);

-- Natural key for idempotent upsert at item level.
-- Use a UNIQUE INDEX (not UNIQUE CONSTRAINT) because expression keys are needed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_order_items_natural
    ON grocery.order_items (
        order_pk,
        item_name_norm,
        COALESCE(unit_qty, -1),
        COALESCE(weight_qty, -1),
        COALESCE(weight_unit, ''),
        COALESCE(unit_cost, -1),
        COALESCE(line_total, -1)
    );

CREATE OR REPLACE FUNCTION grocery.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_orders_set_updated_at ON grocery.orders;
CREATE TRIGGER trg_orders_set_updated_at
BEFORE UPDATE ON grocery.orders
FOR EACH ROW
EXECUTE FUNCTION grocery.set_updated_at();

DROP TRIGGER IF EXISTS trg_order_items_set_updated_at ON grocery.order_items;
CREATE TRIGGER trg_order_items_set_updated_at
BEFORE UPDATE ON grocery.order_items
FOR EACH ROW
EXECUTE FUNCTION grocery.set_updated_at();

COMMIT;
