# Receipt backlog processing

The idempotent receipt backlog processor discovers receipt files, skips source
hashes already represented in receipt evidence, processes pending receipts
through the shared OCR/parser/persistence path, and records per-receipt status
for retries and operations.

## Manual run

With the database and receipt storage reachable:

```bash
ledger receipts process /data/receipts/raw/scanned/inbox --workers 2
```

Configuration can be supplied with `DATABASE_URL`, `RECEIPT_SOURCE_ROOT`, and `HOME_BUDGET_OCR_CACHE`. The process reports discovered, skipped, succeeded, failed, and review-required counts. A PostgreSQL advisory lock prevents overlapping backlog runs.

Pass `--refresh-ocr-cache` to bypass cached OCR and reprocess every discovered
receipt, including receipts already marked succeeded or review-required.

## Receipt-scoped product enrichment

After import, product enrichment can be limited to all line items belonging to
one receipt. A numeric `--receipt` selector is the canonical
`budget.expenses.id`, not `budget.receipt_evidence.id`. The command requires
`DATABASE_URL` and `BRAVE_SEARCH_API_KEY`; `OPENAI_API_KEY` enables the AI query
expansion fallback.

Run a dry run first:

```bash
ledger receipts enrich --receipt 1
```

The dry run performs searches and prints a JSON summary without saving results,
so the Ledger UI continues to show `Not enriched`. Check that the reported
`item_ids` belong to the intended expense, then persist accepted matches:

```bash
ledger receipts enrich \
  --receipt 1 \
  --write-db
```

The summary includes `considered`, `searched`, `db_hits`, `ai_queries`,
`ai_expanded`, `accepted`, `review`, and `unsupported`. `review` is the number
of matches below the automatic confidence threshold; it does not open an
interactive prompt. Those items remain unenriched for manual review in Ledger.

## Kubernetes

In the base `k8s` deployment, `k8s/receipt-processor-cronjob.yaml` runs local
backlog processing every 15 minutes. `concurrencyPolicy: Forbid` prevents
Kubernetes from starting a second scheduled job while the previous job is still
running, and the PostgreSQL advisory lock provides an additional guard against
manual or accidental concurrent runs.

The job uses one Python worker so the medium PaddleOCR models are loaded only once. PaddleOCR contributes high-confidence line candidates to the Tesseract DPI/layout consensus and falls back cleanly when unavailable. The pod requests 2 CPUs and 4Gi memory and is limited to 4 CPUs and 8Gi memory. The K3s node must have enough capacity for the measured Paddle peak plus PostgreSQL, Ledger, and system workloads.

The shared `home-budget-data` PVC is mounted at `/data`, so raw receipts and the OCR cache are available to the processor. Database credentials come from `postgres-secret`.

The optional `deploy/rabbitmq` overlay keeps the same schedule but changes the
CronJob to publish work and adds the long-running `receipt-worker` Deployment.
See the [RabbitMQ runbook](rabbitmq-receipts.md) for rollout, monitoring, and
recovery.

Useful commands:

```bash
kubectl -n home-budget get cronjob receipt-processor
kubectl -n home-budget create job --from=cronjob/receipt-processor receipt-processor-manual
kubectl -n home-budget logs job/receipt-processor-manual
kubectl -n home-budget get jobs
```

## Ledger visibility

Open `/ledger/receipt-processing` to view the latest status for each receipt
source hash, including attempt count, last attempt time, completion time, and
last error. Failed receipts retry until their attempt budget is exhausted; an
operator can then reset them with `ledger receipts retry` after fixing the
cause. Successful receipts are skipped on subsequent discovery runs.

Receipt processing status is operational metadata only. Canonical expenses, evidence, duplicate review, and reconciliation remain available through their existing Ledger pages.
