# Receipt backlog processing

KAN-82 adds an idempotent receipt backlog processor that discovers receipt files, skips source hashes already represented in receipt evidence, processes pending receipts through the existing OCR/parser/persistence path, and records per-receipt status for retries and operations.

## Manual run

With the database and receipt storage reachable:

```bash
home-budget-process-receipts /data/receipts/raw/scanned/inbox --workers 2
```

Configuration can be supplied with `DATABASE_URL`, `RECEIPT_SOURCE_ROOT`, and `HOME_BUDGET_OCR_CACHE`. The process reports discovered, skipped, succeeded, failed, and review-required counts. A PostgreSQL advisory lock prevents overlapping backlog runs.

## Kubernetes

`k8s/receipt-processor-cronjob.yaml` runs every 15 minutes. `concurrencyPolicy: Forbid` prevents Kubernetes from starting a second scheduled job while the previous job is still running, and the PostgreSQL advisory lock provides an additional guard against manual or accidental concurrent runs.

The job starts with two Python worker processes for OCR/parsing. The pod requests 500m CPU and 512Mi memory and is limited to 2 CPUs and 2Gi memory. Tune workers only after observing real workload usage with `kubectl -n home-budget top pod`.

The shared `home-budget-data` PVC is mounted at `/data`, so raw receipts and the OCR cache are available to the processor. Database credentials come from `postgres-secret`.

Useful commands:

```bash
kubectl -n home-budget get cronjob receipt-processor
kubectl -n home-budget create job --from=cronjob/receipt-processor receipt-processor-manual
kubectl -n home-budget logs job/receipt-processor-manual
kubectl -n home-budget get jobs
```

## Ledger visibility

Open `/ledger/receipt-processing` to view the latest status for each receipt source hash, including attempt count, last attempt time, completion time, and last error. Failed receipts remain retryable; successful receipts are skipped on subsequent discovery runs.

Receipt processing status is operational metadata only. Canonical expenses, evidence, duplicate review, and reconciliation remain available through their existing Ledger pages.
