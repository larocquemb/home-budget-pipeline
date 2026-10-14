"""Hash-keyed receipt catalogue shared by Ledger and graph projection."""

# Keep pending OCR discoveries and evidence imported without the OCR queue.
RECEIPT_CATALOGUE = """(
    SELECT source_sha256, source_reference, first_discovered_at, updated_at
      FROM ingest.receipts
    UNION ALL
    SELECT e.source_sha256, e.source_reference, e.created_at, e.updated_at
      FROM budget.receipt_evidence e
     WHERE NOT EXISTS (SELECT 1 FROM ingest.receipts r
                        WHERE r.source_sha256 = e.source_sha256)
)"""
