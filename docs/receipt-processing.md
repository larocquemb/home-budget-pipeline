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

Mac development and K3s use separate OCR caches:

| Environment | Cache location |
| --- | --- |
| Mac | `.ocr_cache` in the local repository, configured by `HOME_BUDGET_OCR_CACHE` in `.env.dev`. |
| K3s | `HOME_BUDGET_OCR_CACHE` in `.env.k3s`, currently `/data/receipts/derived/ocr-cache` on the `home-budget-data` PVC. |

Load the Mac settings before running local processing or cache rebuilds:

```bash
set -a
source .env.dev
set +a
```

Keep the Mac cache on local disk, even when reading source receipts from the
shared SMB folder. OCR creates cache files as needed. Switching the Mac cache
location leaves existing PV cache files intact; only K3s continues using them.
Already-running Mac processes retain their previous environment until restarted.

`make deploy-k3s` reads the K3s cache path from `.env.k3s` and writes it to the
`receipt-runtime-config` ConfigMap before starting Argo CD sync. The receipt
CronJob, RabbitMQ worker, and manual scanned-receipt Job read that ConfigMap.
Use a directory below `/data` so the cache stays on the PV. The ConfigMap is
provisioned outside Git, so Argo CD does not overwrite the chosen value.
Changed values request Deployment restarts through Argo CD; future Jobs use the updated setting,
while already-running Jobs finish with their original setting. Existing cache
files are neither moved nor deleted when the path changes.

To check or provision only these runtime settings:

```bash
make k3s-config-check
make k3s-config-apply
```

Provision this ConfigMap before deploying these manifests for the first time.

Pass `--refresh-ocr-cache` to bypass cached OCR and reprocess every discovered
receipt, including receipts already marked succeeded or review-required.

To reprocess exactly one receipt, use its full path relative to the configured
receipt source root:

```bash
ledger receipts reprocess '2026-08-14/receipts_20260814_0001.pdf' --verbose
```

This runs immediately with one OCR worker, refreshes that receipt's OCR cache,
and replaces its canonical extraction and line items, even if it already
succeeded. It preserves the date-relative source reference and takes the same
per-receipt PostgreSQL lock as the queue consumer. It does not publish RabbitMQ
messages. The JSON result reports `succeeded` or `review_required`; failures
return a nonzero exit code.

A successful run ends with:

```json
{
  "source_reference": "2026-08-14/receipts_20260814_0001.pdf",
  "status": "succeeded"
}
```

This confirms that processing and persistence completed for that receipt.
`review_required` means processing completed but the extraction needs review.

Defaults come from `RECEIPT_SOURCE_ROOT`, `HOME_BUDGET_OCR_CACHE`, and
`DATABASE_URL` (or `HOME_BUDGET_PG_DSN`). Override them with `--receipt-root`,
`--ocr-cache`, and `--db-dsn`; custom schemas use `--ingest-schema` and
`--budget-schema`.

In K3s, after deploying an image containing this command through Argo CD:

```bash
kubectl --context brownrook-k3s1 -n home-budget \
  exec deployment/receipt-worker -- \
  ledger receipts reprocess '2026-08-14/receipts_20260814_0001.pdf' --verbose
```

Run this when the worker is idle so the extra OCR process has memory available.
Use `receipts retry` for failed or interrupted work that should become eligible
for normal publishing again.

| Command | Scope | Result |
| --- | --- | --- |
| `receipts retry SOURCE_REFERENCE` | One failed or interrupted receipt | Resets its attempt budget for the next publish or process run; refuses completed receipts. |
| `receipts reprocess SOURCE_REFERENCE` | One receipt, including completed receipts | Immediately refreshes OCR and replaces its extraction results. |
| `receipts process --refresh-ocr-cache` | Every receipt under the supplied root | Immediately refreshes OCR and replaces extraction results for the whole selection. |

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
