"""Hash-keyed receipt catalogue shared by Ledger and graph projection."""

# Keep pending OCR discoveries and evidence imported without the OCR queue.
RECEIPT_CATALOGUE = """(
    SELECT c.*,COALESCE((
        SELECT (ev.payload->'after_source_archived')='true'::jsonb
          FROM lineage.receipt_events ev WHERE ev.source_sha256=c.source_sha256
           AND ev.payload->>'event'='receipt_source_reconciled'
         ORDER BY ev.occurred_at DESC,ev.id DESC LIMIT 1),FALSE) AS source_archived
    FROM (
    SELECT source_sha256, source_reference, first_discovered_at, updated_at
      FROM ingest.receipts
    UNION ALL
    SELECT e.source_sha256, e.source_reference, e.created_at, e.updated_at
      FROM budget.receipt_evidence e
     WHERE NOT EXISTS (SELECT 1 FROM ingest.receipts r
                        WHERE r.source_sha256 = e.source_sha256)
    ) c
)"""
