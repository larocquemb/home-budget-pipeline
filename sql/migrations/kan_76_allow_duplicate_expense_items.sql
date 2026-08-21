BEGIN;

-- A receipt can legitimately contain the same item more than once with the
-- same quantity and price. Idempotency for scanned receipt ingestion is
-- handled by replacing the expense's item rows during upsert, so this natural
-- key must not reject repeated lines from a single receipt.
DROP INDEX IF EXISTS budget.uq_expense_items_natural;

COMMIT;
