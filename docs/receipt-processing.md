# Receipt backlog processing

All receipt processing and cache rebuild commands submit work through RabbitMQ.
Publishers discover files and queue requests; consumers run OCR and persistence.
There is no direct-processing fallback when the broker is unavailable.

## Manual run

With RabbitMQ, the database, receipt storage, and a consumer available:

```bash
ledger receipts process /data/receipts/raw/scanned/inbox
```

`receipts process` is an alias for queue publication. Configure `RABBITMQ_URL`,
`DATABASE_URL`, `RECEIPT_SOURCE_ROOT`, and `HOME_BUDGET_OCR_CACHE`. Its summary
reports discovered, skipped, exhausted, cache-missing, and published counts.
Completion appears in consumer logs. Per-receipt PostgreSQL locks serialize
work across consumers. Start a consumer with `ledger receipts consume`.
The legacy `home-budget-process-receipts` and `home-budget-scan` entry points
use the same `receipts process` arguments and queue dispatch; old synchronous
report options such as `--json` and `--show-review` are no longer supported.

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
`budget.receipt_reprocess_requests` and `budget.receipt_cache_requests` without
rebuilding existing ingest state. Workers accept normal v1, full reprocess v2,
and cache-only v3 messages. Finish rolling out consumers before sending new
message versions; old consumers send unsupported messages to the dead queue.
Then publish:

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
| `receipts process --refresh-ocr-cache` | Every receipt under the supplied root | Queues one full reprocess request per receipt; workers replace extraction results. |

## Who checks for missing OCR cache files?

The **`receipt-processor` CronJob** runs `ledger receipts publish` every
15 minutes (`*/15 * * * *`). This publisher discovers source files under
`RECEIPT_SOURCE_ROOT`, reads their PostgreSQL completion status, and checks for
their cache files under `HOME_BUDGET_OCR_CACHE`.

| Component | Responsibility |
| --- | --- |
| `receipt-processor` publisher | Detect eligible inbox receipts, including completed receipts whose OCR cache is missing, and publish requests. |
| RabbitMQ (`receipts.v1.work`) | Hold the requests until a consumer receives them. |
| `receipt-worker` consumer | Run OCR, recreate cache files, replace saved extraction results for full reprocess requests, and acknowledge completion. |

If a completed receipt's cache is missing, the publisher queues a full reprocess
request through RabbitMQ. The worker recreates the cache and replaces saved
extraction results and line items. The publisher never runs OCR, and the worker
does not scan the inbox independently for missing caches.
Both `succeeded` and `review_required` receipts are eligible. Existing current
or legacy cache files keep completed receipts skipped. The publication summary
reports these requests as `cache_missing` (included in `published`).

Deleting the cache beneath an inbox therefore makes its completed receipts
eligible for reprocessing on the next 15-minute publisher run. This does not
require flushing PostgreSQL. The publisher and consumers must use the same
cache directory; an incorrect publisher cache path could trigger unwanted
reprocessing.

The scheduled check runs only while the CronJob is enabled (`suspend: false`).
For an immediate check, run `ledger receipts publish` with the same source,
database, and cache settings. `ledger receipts process` uses the same check.
Both commands submit work through RabbitMQ and return after publication;
consumer logs report processing completion.

## Rebuild only the OCR cache

To rebuild only the cache without changing database extraction results, use the
following command. Pause scheduled publishing through Git and Argo CD before
deleting caches for this purpose, and resume after the rebuild completes:

```bash
ledger ocr-cache rebuild --verbose
```

In K3s, run:

```bash
kubectl --context brownrook-k3s1 -n home-budget \
  exec deployment/receipt-worker -- \
  ledger ocr-cache rebuild --verbose
```

This publishes one `receipt.cache-rebuild.v3` message per discovered source,
including receipts already completed in PostgreSQL. `--verbose` prints confirmed
publication progress, for example `[1/12] Queued 2026-08-14/receipts_20260814_0001.pdf`.
The JSON summary reports `published`, the batch `request_id`, and `status: queued`.
It confirms submission, not cache completion. Watch consumer logs for
`Rebuilding OCR cache`, `cache_rebuilt`, and retry/dead-letter outcomes.

The worker uses its own `HOME_BUDGET_OCR_CACHE`, rebuilds one receipt at a time,
and records request completion without changing saved extraction or ingest
completion state. `--workers` is deprecated and does not start local processes;
concurrency comes from consumer replicas. The legacy rebuild `--ocr-cache`
option does not override the consumer's cache directory.

For an interrupted or uncertain batch publication, rerun with the printed
`--request-id UUID`. The same batch and source produce the same request identity,
so already-completed requests are skipped. A new invocation without that UUID
creates a fresh rebuild. Partial publication failures report how many messages
were confirmed. Workers retry each request at most three times before dead-lettering.
Full-inbox refresh with `receipts process --refresh-ocr-cache` uses the same batch
publication and retry behavior, but sends v2 requests that replace extraction.

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

`k8s/receipt-processor-cronjob.yaml` publishes eligible receipts every 15 minutes.
`concurrencyPolicy: Forbid` prevents overlapping scheduled discovery runs. The
publisher mounts receipt storage read-only and requests 100m CPU and 256Mi memory.
The manual scanned-receipt Job and default container command also publish work.

Deploy `deploy/rabbitmq` through Argo CD to supply the broker and long-running
`receipt-worker` Deployment. Base manifests require the same RabbitMQ secret and
a running consumer. The worker handles one receipt at a time with shared PV
storage and per-receipt database locks. It requests 2 CPUs and 4Gi memory, with
limits of 4 CPUs and 8Gi. See the [RabbitMQ runbook](rabbitmq-receipts.md) for
rollout, monitoring, and recovery.

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
cause. Queued publishing skips completed receipts only while their OCR cache
file exists; otherwise it queues full reprocessing. `receipts process`
uses this same publication policy. `--refresh-ocr-cache` explicitly queues
full reprocessing for every discovered receipt.

Receipt processing status is operational metadata only. Canonical expenses, evidence, duplicate review, and reconciliation remain available through their existing Ledger pages.
