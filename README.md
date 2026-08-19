# home-budget-pipeline

## Safe Postgres Auth

Avoid putting DB passwords in shell history or command arguments.

Use `~/.pgpass` (recommended):

```bash
cat > ~/.pgpass <<'EOF'
localhost:5432:home_budget:postgres:YOUR_PASSWORD
EOF
chmod 600 ~/.pgpass
```

Then run the loader without inline password:

```bash
.venv/bin/python3 instacart_orders_extract_persistent.py --write-db --db-dsn "host=localhost port=5432 dbname=home_budget user=postgres"
```

Alternative: set `HOME_BUDGET_PGPASSWORD` (script maps it to `PGPASSWORD` if needed), but `~/.pgpass` is preferred.

## Scanned paper receipts (KAN-76)

Apply the scan metadata migration once:

```bash
psql -d home_budget -f sql/KAN-76_scanned_receipts.sql
```

Preview an archive without changing the database:

```bash
.venv/bin/python3 scanned_receipt_ingest.py /path/to/scanned-receipts --json scanned_receipts_report.json
```

The summary's `discovered` value should equal the number of supported scan files (64 for the KAN-76 archive). Receipts with missing totals/items or low extraction confidence are reported as `review`; scans with no OCR text are `unreadable`.

After reviewing the JSON report, ingest the archive idempotently:

```bash
.venv/bin/python3 scanned_receipt_ingest.py /path/to/scanned-receipts \
  --write-db \
  --db-dsn "host=localhost port=5432 dbname=home_budget user=postgres"
```

Each scan uses its SHA-256 content hash as the canonical scanned `order_id`, so rerunning the same file updates the existing `budget.expenses` row instead of creating a duplicate. Original filename/reference, hash, page OCR, merged OCR, extraction status, and confidence are retained for traceability. Adjacent PDF pages are merged with repeated boundary lines removed so overlapping long-receipt scans do not duplicate those line items.

Run the focused unit tests with:

```bash
.venv/bin/python3 -m unittest tests.test_scanned_receipt_ingest
```
