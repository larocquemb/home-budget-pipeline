-- Current bootstrap constraint policy for rebuildable databases.
-- Identical receipt lines are valid (for example, two separately scanned items
-- with the same description and price), so expense_items must not enforce
-- uniqueness on item content.
--
-- OCR can occasionally produce very large item_name values. PostgreSQL btree
-- indexes cannot store arbitrarily large text keys, so index a fixed-width hash
-- for exact normalized-name lookups instead of item_name_norm itself.

BEGIN;

DROP INDEX IF EXISTS budget.uq_expense_items_natural;
DROP INDEX IF EXISTS budget.idx_expense_items_name_norm;

CREATE INDEX IF NOT EXISTS idx_expense_items_name_norm_hash
    ON budget.expense_items (md5(item_name_norm));

COMMIT;
