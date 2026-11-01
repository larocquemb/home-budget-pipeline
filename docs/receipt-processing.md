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
work across consumers. Start a worker with `ledger receipts consume` and a result
collector with `ledger receipts collect`; the collector commits OCR results and
extracted expenses to PostgreSQL.
The legacy `home-budget-process-receipts` and `home-budget-scan` entry points
use the same `receipts process` arguments and queue dispatch; old synchronous
report options such as `--json` and `--show-review` are no longer supported.

Mac development and K3s use separate OCR caches:

| Environment | Cache location |
| --- | --- |
| Mac | `.ocr_cache` in the local repository, configured by `HOME_BUDGET_OCR_CACHE` in `.env.dev`. |
| K3s | `HOME_BUDGET_OCR_CACHE` in `.env.k3s`, currently `/data/receipts/derived/ocr-cache` on the `home-budget-data` PVC. |

On the Mac, `/Volumes/brownrook/home-budget/receipts/derived/ocr-cache`
is the shared production cache mounted by K3s at
`/data/receipts/derived/ocr-cache`. It is separate from the development
repository's `.ocr_cache`. The same source receipt can have a JSON result in
each cache, with different OCR run IDs and processing times. These are separate
environment results; do not merge them or delete one as a duplicate.

Each result keeps the source filename and adds `.json`, for example
`0050_261031035511_001.tif.json`. It contains extracted text, page results and
OCR metadata. A cache file proves a saved OCR result exists; it does not prove
that financial receipt import completed. Retired validation inputs and outputs
belong in the private per-story evidence archive, outside the active cache root.

Select Mac settings explicitly for application commands:

```bash
ledger --env-file .env.dev receipts process --verbose
ledger --env-file .env.dev ocr-cache rebuild --missing-only --verbose
```

Use `ledger --env-file .env.dev ocr-cache clean --local` to empty the local
cache while retaining prior outputs privately. It stops local receipt services;
restart them with `ledger --env-file .env.dev receipts services start` before
queueing work. `make` is used for build/test and infrastructure tooling; application
operations use `ledger`. Commands without `--env-file` below assume their runtime
already supplies the correct environment (for example a Kubernetes worker).

Keep the Mac cache on local disk, even when reading source receipts from the
shared SMB folder. OCR creates cache files as needed. Switching the Mac cache
location leaves existing PV cache files intact; only K3s continues using them.
Already-running Mac processes retain their previous environment until restarted.

For development, PostgreSQL and RabbitMQ run in isolated Podman services on
Arsene (ports 15432 and 15673); Python workers and their OCR cache stay on the
Mac. Enable the worker and collector as
background services with `ledger --env-file .env.dev receipts services start`, then submit work with
`ledger --env-file .env.dev receipts process --verbose`. Check them with `ledger --env-file .env.dev receipts services status`
and follow their output with `ledger --env-file .env.dev receipts services logs`. After changing `.env.dev`,
run `ledger --env-file .env.dev receipts services restart`. Do not load the obsolete
`.local-postgres/runtime.env`. Enable local log forwarding with `make dev-telemetry-start`; see the
[local development guide](local-development.md#send-local-logs-and-opentelemetry-to-grafana).
See [local macOS development](local-development.md) for setup and lifecycle commands.

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

When OCR is already available and only extraction needs updating, use:

```bash
ledger --env-file .env.dev receipts reprocess 'YYYY-MM-DD/receipt.pdf' --use-ocr-cache
```

This requires a readable cache matching the source SHA-256. It runs current
extraction rules and publishes results directly to the durable collector,
without executing OCR or changing cache bytes. Original OCR run/pass identities
are retained. The collector replaces extraction through the normal reprocess
path, preserving manual allocation protections and validation warnings. Missing
or mismatched caches fail instead of silently running OCR. Publication is
asynchronous; check the saved Ledger result after collection.

To re-extract all existing caches in one command:

```bash
ledger --env-file .env.dev receipts reprocess-cache --apply
```

Omit `--apply` to preview. Add `--source-prefix electronic/` or a dated source
folder to limit the selection. Both configured scanned and electronic inputs
are included by default. Deleted sources and missing caches are skipped;
invalid, mismatched or incomplete caches are reported as failures and never
fall back to OCR. The summary reports eligible, published, missing and failed
counts. Reuse its `--request-id UUID` after uncertain publication to retain
stable per-receipt request identities. One broker connection publishes the
batch to the collector; completion remains asynchronous. Applied batches record
per-receipt progress for Grafana, including confirmed cache-result publication,
validation failures and collector persistence. Preview writes no progress history.
Cache bytes are retained.

Merchant identifies the retailer; Store identifies the printed branch or
location. They are separate fields in receipt extraction, APIs and the receipt
list/detail pages. Unknown or conflicting store numbers remain blank. A
co-branded payment card is not evidence of the merchant. The store is retained
as typed receipt metadata without renaming the legacy canonical `store_name`
column. New result readers accept older results without a store field; deploy
or reload collectors before workers emitting the new optional field.
Merchant extraction also reads explicit issuer names from source PDF titles and
receipt filenames (for example `Example Company receipt [#123].pdf`) and email
receipt subjects. Recipient email addresses are excluded. A new unreadable
merchant does not erase a previously populated merchant. Retained-text identity
refresh consults only matching original PDFs, checking their source SHA first;
PDF creation/print dates are not used as purchase dates.

To preview identity extraction from retained database text without rerunning OCR
or rewriting purchases, dates, amounts or items:

```bash
ledger --env-file .env.dev receipts refresh-identities
ledger --env-file .env.dev receipts refresh-identities --apply --expected-version VERSION_FROM_PREVIEW
```

An optional exact source reference scopes this command to one document. Identity
changes are audited and explicit corrected-field feedback is preserved.

To reconcile deleted or moved source files with the database and active cache:

```bash
ledger --env-file .env.dev receipts refresh --source-prefix electronic/2026-10-27/
ledger --env-file .env.dev receipts refresh --source-prefix electronic/2026-10-27/ --apply --expected-version VERSION_FROM_PREVIEW
```

Use an existing dated source folder. Refresh verifies the mounted folder,
archives missing/replaced sources, restores matching returned files and removes
stale OCR files from the active cache only after the DB reconciliation commits.
Cache bytes are retained outside the active cache in a private archive, with a
hash manifest. Pending discoveries without OCR evidence are archived through
lineage without creating fake receipt evidence. Active receipt/processing
lists exclude archived sources; history and accounting transactions remain.
This is an explicit refresh command, not a background deletion monitor. New
files are queued separately with `receipts process`.

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
It confirms submission, not cache completion. Watch worker logs for
`status=active`, `status=cache_ready`, `cache_results_published`, and
retry/dead-letter outcomes; the collector reports durable `cache_rebuilt`
completion after its database commit.

Limit a repair to one path relative to the receipt root, or skip receipts that
already have a current or legacy cache:

```bash
ledger ocr-cache rebuild \
  --source-reference 2026-08-14/receipts_20260814_0001.pdf \
  --verbose

ledger ocr-cache rebuild --missing-only --verbose
```

Each v3 message carries the batch UUID plus its item index and total. Worker
logs include `batch_id`, `progress`, and `completed=N/TOTAL`. The completed
count comes from atomic markers on the shared receipt volume, so concurrent
pods report one monotonic batch total without requiring database access.

The worker uses its own `HOME_BUDGET_OCR_CACHE` and rebuilds one receipt at a
time. A shared-PV advisory lock serializes cache writes by source hash. After an
atomic cache replacement, the worker writes a request marker containing the
cache checksum. A duplicate delivery reuses that exact cache and republishes
the idempotent result events instead of repeating OCR. Deleting or changing the
cache invalidates the marker, allowing a new repair to recreate it. Collector
persistence records request completion without changing saved extraction or
ingest completion state. `--workers` is deprecated and does not start local
processes; concurrency comes from consumer replicas. The rebuild `--ocr-cache`
option is used only by the publisher for `--missing-only`; consumers always use
their own configured cache directory.

For an interrupted or uncertain batch publication, rerun with the printed
`--request-id UUID`. The same batch and source produce the same request identity,
so already-completed requests are skipped. A new invocation without that UUID
creates a fresh rebuild. Partial publication failures report how many messages
were confirmed. Workers retry each request at most three times before dead-lettering.
Full-inbox refresh with `receipts process --refresh-ocr-cache` uses the same batch
publication and retry behavior, but sends v2 requests that replace extraction.

## Receipt product codes

Costco item rows show a separate Product code column. The original stored
receipt reading is preserved; the API exposes `product_code`,
`receipt_item_name` and `discount_for_product_code` separately. Existing rows
with a printed code prefix update immediately. Older electronic-import SKUs
are recovered only from an unambiguous retained item name/amount match.

Product matching searches the code with the printed description. An exact
code requires corroborating name evidence on a retailer product page; an
explicit conflicting item code prevents acceptance. `TPD/<code>` adjustments
retain their own code and reference the discounted product without being
searched as products. Amounts and accounting allocation are preserved.

## Receipt-scoped product enrichment

After import, product enrichment can be limited to all line items belonging to
one receipt. A numeric `--receipt` selector is the canonical
`budget.expenses.id`, not `budget.receipt_evidence.id`. The command requires
`DATABASE_URL` and `BRAVE_SEARCH_API_KEY`; `OPENAI_API_KEY` enables the AI query
expansion fallback.

The base OCR pass supplies row order and layout. Ordinary product enrichment
also evaluates distinct spellings from all retained successful passes in the
latest run for each source. It associates rows through unique ordered text
matches with price checks, rather than equating OCR output line numbers with
Ledger item numbers. Missing duplicate purchases and ambiguous alignments are
excluded. These associations are textual evidence, not verified physical image
coordinates; very different readings that cannot be aligned remain excluded.

All aligned spellings are searched before AI query expansion. Product scores
use the spelling that supplied the search, and close competing products from
different spellings require review. Saved results retain the readings, source
run/pass/page/line IDs, evaluated product candidates, and selected spelling in
`budget.product_enrichment_results.ocr_evidence`. Accepted spelling changes do
not alter the original OCR evidence. Existing OCR caches can be reused.

For an existing database, apply the additive schema update without resetting it:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/product_enrichment.sql
```

Then rerun receipt enrichment. Explicit receipt selection reevaluates accepted
results that predate OCR alternative evidence once; newly accepted results
continue to be skipped on subsequent runs. Each additional distinct spelling
can add search requests, although repeated queries use the Brave cache.

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
of matches below the automatic confidence threshold or with competing OCR
product interpretations; it does not open an
interactive prompt. Those items remain unenriched for manual review in Ledger.

## Kubernetes

### Review a collaborative product recommendation

The receipt-scoped CLI above evaluates aligned OCR alternatives with retailer
searches. To also pool receipt images, local Qwen proposals and Brave evidence,
use the Kubernetes collaboration job:

```sh
make enrich-products COLLABORATE=1 LIMIT=1 RECEIPT='receipt.pdf' DRY_RUN=1
make enrich-products COLLABORATE=1 LIMIT=1 RECEIPT='receipt.pdf'
```

Replace the filename with your receipt. `LIMIT=1` bounds the run to one item;
it does not process every item on that receipt. The first command validates the
Job without creating it; the second saves collaborative evidence for review.
Open the receipt in Ledger, inspect the recommendation, and explicitly accept
it for the selected item. Saved recommendations do not change accepted products.
Duplicate rows need separate acceptance unless the receipt-wide **Apply accepted
matches and category rules** action can reuse an accepted exact retailer/text cache
match. That action also repairs unresolved categories without provider calls.

An accepted reading correction leads the row; **Import history** retains the
imported text. **Related OCR observations** shows different retained readings
without claiming that nearby OCR lines belong to that exact Ledger row. See the
[user guide](user-guide.md#understand-item-readings-and-product-recommendations)
and [collaboration manual](product-model-comparison.md) for evidence and model
configuration. After a new run or acceptance, use `make receipts-graph` and open
the receipt's **View receipt knowledge graph** link to inspect the updated story.

### Scheduled discovery

`k8s/receipt-processor-cronjob.yaml` publishes eligible receipts every 15 minutes.
`concurrencyPolicy: Forbid` prevents overlapping scheduled discovery runs. The
publisher mounts receipt storage read-only and requests 100m CPU and 256Mi memory.
The manual scanned-receipt Job and default container command also publish work.

Deploy `deploy/rabbitmq` through Argo CD to supply the broker, long-running
`receipt-worker` Deployment, and its KEDA `ScaledObject`. The `brownrook-root`
GitOps application manages the KEDA operator and CRDs. Base manifests require
the same RabbitMQ secret and a running consumer. Each worker handles one receipt
at a time with shared-PV locks. A worker requests 1 CPU and 4Gi memory, with
limits of 4 CPUs and 8Gi. KEDA keeps one worker warm and scales to at most two,
for a maximum reservation of 2 CPUs and 8Gi. See the
[RabbitMQ runbook](rabbitmq-receipts.md) for rollout, monitoring, scaling
validation, and recovery.

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

## Paddle CPU runtime and acceleration

Linux/x86 production uses PaddlePaddle 3.2.2 with MKL-DNN/oneDNN enabled.
PaddlePaddle 3.3.1 regresses the PIR oneDNN attribute converter used by the
PP-OCRv6 models; the application pins the compatible CPU wheel instead of
turning acceleration off. Other platforms retain their existing runtime pin.
See the [upstream regression](https://github.com/PaddlePaddle/Paddle/issues/77340).

The application image runs actual detection and recognition on synthetic text
as part of its build. Loading the model alone did not expose this failure.
Run the same check locally with `make test-paddle-runtime`. OCR pass metadata
records `enable_mkldnn` alongside model names. Old failed pass evidence remains
retained; a runtime upgrade does not rewrite existing OCR caches or history.

## Merchant header corroboration

Retailer names can contain short components separated by `&` or `+`. Address,
contact and product rows are not fallback merchant names. When a selected
transcript loses its merchant header, extraction can recover a name observed
in at least two successful first-page OCR passes and corroborated by a printed
website. A footer website alone never introduces a merchant; tied names remain
unresolved. No receipt filename, hash, amount or ID selects a special rule.

`receipts refresh-identities` uses current-run OCR passes and hash-verified
retained artifacts for this metadata-only refresh. For a local mount of another
environment's cache, use `--ocr-cache PATH --artifact-uri-prefix PREFIX` with
that environment's explicit settings. Preview and expected-version checks
remain required; OCR-pass evidence contributes to the plan version/audit hash.
Dates, amounts, item rows and category decisions are preserved.
