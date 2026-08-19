-- KAN-76: time-bounded payment-card ownership/reference data.
-- Apply after sql/schema_phase1.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS budget.payment_cards (
    id BIGSERIAL PRIMARY KEY,
    card_label TEXT NOT NULL,
    card_network TEXT NOT NULL,
    card_last4 TEXT NOT NULL CHECK (card_last4 ~ '^[0-9]{4}$'),
    owner_name TEXT NOT NULL,
    issuer TEXT,
    valid_from DATE NOT NULL,
    valid_to DATE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_payment_cards_lookup
    ON budget.payment_cards (lower(card_network), card_last4, valid_from, valid_to);

CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_cards_version
    ON budget.payment_cards (lower(card_network), card_last4, valid_from, owner_name);

DROP TRIGGER IF EXISTS trg_payment_cards_set_updated_at ON budget.payment_cards;
CREATE TRIGGER trg_payment_cards_set_updated_at
BEFORE UPDATE ON budget.payment_cards
FOR EACH ROW
EXECUTE FUNCTION budget.set_updated_at();

COMMIT;
