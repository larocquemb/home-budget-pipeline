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

This publishes one persistent, confirmed message to `receipts.v1.work`. The
existing queue consumer refreshes that receipt's OCR cache and replaces its
canonical extraction and line items, even if it already succeeded. The
date-relative source reference is preserved. The command exits after RabbitMQ
confirms publication; OCR runs asynchronously in the consumer.

A successful publication ends with output like:

```json
{
  "source_reference": "2026-08-14/receipts_20260814_0001.pdf",
  "source_sha256": "<receipt SHA-256>",
  "request_id": "740023b1-a078-4914-b074-81bd7129bb75",
  "queue": "receipts.v1.work",
  "status": "queued"
}
```

`queued` confirms broker acceptance, not completed extraction. Worker logs report
OCR and persistence progress, then `succeeded`, `review_required`, or a retry/dead
queue routing result. `review_required` means the extraction needs review.

The publisher uses `RECEIPT_SOURCE_ROOT` and `RABBITMQ_URL`, with overrides
`--receipt-root` and `--rabbitmq-url`. It requires source-file access but no
database connection. The consumer's configuration supplies `HOME_BUDGET_OCR_CACHE`,
the database DSN, and schemas; the publisher cannot override them.

Each invocation creates a new request UUID. If a publication fails or its
confirmation is lost, retry with the printed `--request-id UUID` to reuse that
request. The worker records completion atomically with the extraction under the
receipt lock, so redelivering a completed request skips OCR. Each new request
has a fresh three-attempt database budget, independent of earlier processing.

In K3s, first deploy this version through Argo CD and wait for all workers to
finish rolling out. The PreSync database setup adds
`budget.receipt_reprocess_requests` without rebuilding existing ingest state.
New workers accept both normal v1 messages and reprocess v2 messages; older
workers reject v2 messages to the dead queue. Then publish:

```bash
kubectl --context brownrook-k3s1 -n home-budget \
  exec deployment/receipt-worker -- \
  ledger receipts reprocess '2026-08-14/receipts_20260814_0001.pdf' --verbose
```

Follow the consumer's progress separately:

```bash
kubectl --context brownrook-k3s1 -n home-budget \
  logs -f deployment/receipt-worker --tail=100
```

In [RabbitMQ](https://rabbitmq.brownrook.net), select vhost `receipts` and open
**Queues and Streams → receipts.v1.work**. `Ready` shows waiting requests;
`Unacked` shows delivered work awaiting completion. A fast receipt may only be
visible in message-rate charts. RabbitMQ does not keep an acknowledged-message
history; use the request UUID in worker logs to follow the final result.

Use `receipts retry` for failed or interrupted work that should become eligible
for normal publishing again.

| Command | Scope | Result |
| --- | --- | --- |
| `receipts retry SOURCE_REFERENCE` | One failed or interrupted receipt | Resets its attempt budget for the next publish or process run; refuses completed receipts. |
| `receipts reprocess SOURCE_REFERENCE` | One receipt, including completed receipts | Queues a refresh; the consumer replaces its OCR and extraction results. |
| `receipts process --refresh-ocr-cache` | Every receipt under the supplied root | Immediately refreshes OCR and replaces extraction results for the whole selection. |

## Rebuild only the OCR cache

Deleting cache files does not reset PostgreSQL completion status, so scheduled
publishing will still skip completed receipts. To rebuild the cache without
changing database extraction results:

```bash
ledger ocr-cache rebuild --workers 1 --verbose
```

In K3s, run:

```bash
kubectl --context brownrook-k3s1 -n home-budget \
  exec deployment/receipt-worker -- \
  ledger ocr-cache rebuild --workers 1 --verbose
```

`--verbose` prints the source/cache directories, filenames, and completed/total
counts immediately to stderr:

```text
[0/12] Starting 2026-08-14/receipts_20260814_0001.pdf
[1/12] Completed 2026-08-14/receipts_20260814_0001.pdf
```

With multiple workers, files are first reported as `Queued`, then `Completed`
in completion order. A failure identifies the receipt and exits with an error.
Without `--verbose`, the command prints only its final JSON summary, apart from
OCR library output. The rebuild refreshes every discovered receipt, including
existing caches, and runs directly in the pod rather than through RabbitMQ.
An already-running rebuild will not gain progress output after deployment.

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
