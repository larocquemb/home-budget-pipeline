-- Current bootstrap constraint policy for rebuildable databases.
-- Identical receipt lines are valid (for example, two separately scanned items
-- with the same description and price), so expense_items must not enforce
-- uniqueness on item content.

BEGIN;

DROP INDEX IF EXISTS budget.uq_expense_items_natural;

COMMIT;
