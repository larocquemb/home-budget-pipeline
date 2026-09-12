# BrownRook Ledger / Home Budget Pipeline

BrownRook Ledger is a receipt and household-expense processing pipeline. Its purpose is to turn receipt evidence from multiple sources into a canonical set of purchases and line items that can be categorized, reviewed, aggregated, and eventually exported to a budgeting system such as Vertex42 Money Manager.

The design separates four different concerns:

```text
Receipt evidence  ->  Canonical expense  ->  Categorization  ->  Budget reporting
PDF / scan / web      purchase + items       category/item       category splits
```

A payment account answers **where the money came from**. A budget category answers **what the money was spent on**. Ledger deliberately keeps those concepts separate.

Operational documentation:

- [Online manual](https://larocquemb.github.io/home-budget-pipeline/)
- [Solution architecture](docs/solution-architecture.md)
- [Ledger user guide](docs/user-guide.md)
- [Build, deployment, and recovery runbook](docs/operations-runbook.md)
- [Network access and private-LAN deployment](docs/private-networking.md)
- [Receipt backlog processing](docs/receipt-processing.md)
- [RabbitMQ receipt processing design](docs/rabbitmq-receipt-design.md)
- [RabbitMQ receipt processing runbook](docs/rabbitmq-receipts.md)

---

## 1. Pipeline Overview

The receipt pipeline is organized into several stages.

```text
Receipt source
    |
    v
Discovery / ingestion
    |
    v
OCR + receipt evidence
    |
    v
Normalization / reconciliation
    |
    v
Canonical expense + line items
    |
    v
Category mapping pipeline
    |
    +--> deterministic rules
    +--> approved learned mappings
    +--> optional AI classification
    +--> human review
    |
    v
Persisted category decisions
    |
    v
Category splits / budget export
```

The important architectural principle is that receipt files are **evidence**, while `budget.expenses` represents the canonical financial purchase. Multiple receipt artifacts can therefore describe the same purchase without requiring the purchase to be categorized more than once.

---

## 2. Receipt Sources and Ingestion

Ledger stores canonical expenses using the source codes `instacart`, `costco`,
`sobeys`, and `scanned` on `budget.expenses.source`. Electronic importers retain
their merchant-specific source. PDFs and images processed by the OCR pipeline
use `scanned`, regardless of merchant, and retain their original file as receipt
evidence.

### Scanned paper receipts

Place complete receipt files under `RECEIPT_SOURCE_ROOT`. Discovery walks that
directory recursively and accepts PDF, JPEG, PNG, HEIC/HEIF, and TIFF files.
Configure PostgreSQL with `DATABASE_URL` and optionally set
`HOME_BUDGET_OCR_CACHE`.

For Mac development, `.env.dev` sets `HOME_BUDGET_OCR_CACHE` to the repository's
local `.ocr_cache` directory. K3s uses `/data/receipts/derived/ocr-cache` on its
PV. Load `.env.dev` before running the local CLI so development and deployment
keep separate OCR caches, even when their source receipts are on the same share.

The primary command-line program is `ledger`, matching the web application.
The previous `brownrook` name remains installed as a compatibility alias for
existing scripts. After pulling this change into an existing virtual
environment, reinstall the project once to create `.venv/bin/ledger`:

```bash
python -m pip install -e '.[db]'
ledger --help
```

Submit the backlog through RabbitMQ with the Ledger CLI:

```bash
ledger receipts process --verbose
```

The command discovers eligible receipts and publishes persistent, confirmed
messages. A running `ledger receipts consume` worker performs OCR and persists
results. All receipt processing and cache rebuild commands use RabbitMQ; broker
failure never starts local OCR. Completion is reported in worker logs.

To queue cache rebuilds while preserving saved extraction results:

```bash
ledger ocr-cache rebuild --verbose
```

Run the publisher and consumer separately:

```bash
ledger receipts publish
ledger receipts consume
```

The `receipt-processor` CronJob runs `ledger receipts publish` every 15 minutes.
The publisher checks inbox files, PostgreSQL completion status, and
`HOME_BUDGET_OCR_CACHE`. Completed receipts whose cache is missing are queued
through RabbitMQ; the `receipt-worker` consumer recreates their caches and
replaces saved extraction results and line items. Completed receipts with
current or legacy caches remain skipped. Deleting cache files does not require
a database reset. See [who checks for missing caches](docs/receipt-processing.md#who-checks-for-missing-ocr-cache-files).

Publishing is confirmed and consumers acknowledge manually after a durable
database result or confirmed retry/dead-letter transfer. PostgreSQL advisory
locks serialize duplicate deliveries for the same SHA-256. Use
`ledger receipts retry SOURCE_REFERENCE` after fixing a failed or interrupted
receipt whose attempt budget is exhausted. Enabling RabbitMQ does not require a
database schema rebuild.

See the [receipt backlog guide](docs/receipt-processing.md),
[RabbitMQ design](docs/rabbitmq-receipt-design.md), and
[RabbitMQ runbook](docs/rabbitmq-receipts.md) for configuration and recovery.

---

## 3. OCR, Long Receipts, and Receipt Evidence

Paper receipts may be longer than a scanner platen and therefore may be captured across multiple overlapping pages.

The ingestion pipeline preserves both the extracted information and the evidence used to derive it. Receipt evidence can include:

- scanned PDFs
- photos
- electronic receipts
- email-derived evidence
- OCR text
- page-level OCR payloads
- extracted transaction date/time
- merchant
- total
- payment method and card last four digits
- extraction status and confidence

The tables `budget.receipt_evidence` and `budget.receipt_annotations` allow evidence to exist independently from the canonical expense.

Adjacent scanned pages can be merged while suppressing repeated boundary lines. This prevents overlap between two scans of the same long receipt from creating duplicate receipt items.

Receipt annotations can also preserve human marks such as category labels, separators, subtotals, checkmarks, or notes for later interpretation.

### Scan-to-product detection

Image-based PDFs are rendered at several resolutions. Tesseract evaluates raw
and enhanced images with multiple page-segmentation modes, while optional
PaddleOCR medium models contribute high-confidence product-description lines.
The consensus layer aligns candidates using text similarity, occurrence order,
and monetary amounts. It prefers Paddle text for aligned descriptions, retains
Tesseract amounts and tax codes when necessary, rejects impossible dates, and
uses a high-resolution Tesseract pass for small or damaged glyphs.

After consensus, the parser extracts receipt fields and line items. Missing
merchant, date, and total values can be recovered from a structured filename
such as `20260214_sobeys_363_95.pdf`. The final consensus OCR and parsed receipt
are cached by source hash and persisted as receipt evidence. Individual OCR
passes and their quality metrics are retained for later effectiveness analysis.

The unified Ledger CLI queues cache-only rebuilds through RabbitMQ:

```bash
ledger ocr-cache rebuild --verbose
```

The publisher uses `RECEIPT_SOURCE_ROOT` and `RABBITMQ_URL`; the consumer uses its
configured `HOME_BUDGET_OCR_CACHE` and database. Saved extraction results remain
unchanged; PostgreSQL tracks request attempts and completion. `--verbose` shows
confirmed publication progress. The final JSON reports `published` and `queued`;
worker logs report actual rebuild completion. Retry uncertain publication with
the printed `--request-id UUID`. `--workers` no longer starts local processes;
consumer replicas control concurrency.

Cache version 13 keeps schema, source, processing, timing, and OCR-pass details
inside a top-level `metadata` object. It groups final lines by source page and
records OCR confidence, bounding boxes, line type, and derived department
context. Each OCR pass uses a versioned, engine-neutral metrics envelope
containing status, text, normalized quality measures, engine-specific options,
usage, and provenance. Readers remain compatible with legacy flat cache
formats. This keeps raw layout evidence available for receipt comparison and
later item recognition.

The current payload is organized as follows:

| Location | Contents |
| --- | --- |
| `metadata` | Schema/cache versions, run UUID, relative source reference, SHA-256, UTC processing time, and total duration. |
| `metadata.timings` | `text_extraction_seconds`, `layout_extraction_seconds`, and `structuring_seconds`. |
| `metadata.ocr_passes[]` | Page and pass identity, OCR engine and options, status, duration, selection flag, quality scores, usage, and provenance. |
| `plain_text` | Consensus text reconstructed from the structured page lines. |
| `pages[]` | Source page number and nested lines with text, confidence, type, department context, geometry, indentation, and supporting child lines. |

The [cache writer](src/home_budget_pipeline/receipts/parallel_ingest.py) and
[schema tests](tests/test_receipt_metadata_schema.py) are the authoritative
definition. Existing legacy flat cache files remain readable and are replaced
with the current shape when rebuilt.

`--refresh-ocr-cache` performs a complete refresh: it bypasses cached OCR,
reprocesses receipts already marked complete, and replaces their canonical
extraction and line items. For example:

```bash
ledger receipts process /path/to/receipt/inbox \
  --verbose \
  --refresh-ocr-cache \
  --db-dsn "$DATABASE_URL"
```

Paddle medium models have a substantial memory footprint, so the Kubernetes
receipt processor uses one worker. Workers parallelize separate receipt files;
they do not parallelize stages within one receipt.

To refresh just one receipt, including one already completed, use its exact path
relative to `RECEIPT_SOURCE_ROOT`:

```bash
ledger receipts reprocess '2026-08-14/receipts_20260814_0001.pdf' --verbose
```

This queues a persistent RabbitMQ request and returns `queued`. The consumer
refreshes its OCR cache and replaces its canonical extraction and line items
using the worker's configured database and cache directory. See
[receipt processing](docs/receipt-processing.md) for configuration, deployment,
and progress monitoring in worker logs and the RabbitMQ GUI.

### AI-grounded product enrichment

Product enrichment is a separate post-import stage. It does not alter the raw
receipt evidence. The enrichment command first searches Brave using the literal
OCR item name and the known retailer domain. If that result is weak, the AI
product fallback generates fully expanded search queries from the merchant and
possible OCR spellings. AI output is a search proposal, not product evidence.

Each expanded query is sent back through Brave and must resolve to a recognized
retailer product page with sufficient title/token agreement. Accepted results
store the provider, search query, retailer URL, product title, confidence, and
status in `budget.product_enrichment_results`. Weak or inconsistent matches are
left in review.

Strong verified product evidence can correct a one-glyph brand initialism. For
example, a Sobeys page titled `Old El Paso ...` establishes the initialism
`OEP`; an OCR item named `Cep Pic Med` may therefore be normalized to
`Oep Pic Med` only when the retailer match confidence is at least `0.95`.
The rule is based on the verified product title rather than a receipt-specific
override.

Run enrichment for the pending backlog:

```bash
DATABASE_URL="$HOME_BUDGET_PG_DSN" \
BRAVE_SEARCH_API_KEY="$BRAVE_SEARCH_API_KEY" \
OPENAI_API_KEY="$OPENAI_API_KEY" \
home-budget-enrich-products --limit 100 --threshold 0.85 --write-db
```

For a targeted review or retry, repeat `--item-id` as needed:

```bash
home-budget-enrich-products \
  --item-id 1656 \
  --item-id 1657 \
  --threshold 0.85 \
  --write-db
```

To enrich every line item belonging to one receipt, use the receipt-scoped
module. A numeric selector is the canonical `budget.expenses.id` (not
`budget.receipt_evidence.id`); a filename or path selector is also accepted:

```bash
ledger receipts enrich --receipt 1
```

The command is a dry run unless `--write-db` is supplied. A dry run performs
the searches and prints a JSON summary, but the Ledger UI will continue to show
the items as `Not enriched`. Confirm that `item_ids` contains the expected line
items, then persist accepted matches with:

```bash
ledger receipts enrich \
  --receipt 1 \
  --write-db
```

The JSON summary reports how many items were considered, searched, satisfied
from the database cache, expanded into AI-generated search queries, accepted,
or left for review. `review` is a count of matches below the automatic
confidence threshold; it does not start an interactive prompt. Those items
remain unenriched for manual review in the Ledger UI.

The product-query fallback defaults to `gpt-5.6-terra` and can be changed with
`AI_PRODUCT_MODEL`. Product enrichment currently runs as an explicit command;
receipt import does not automatically invoke external AI or Brave search.

---

## 4. Canonical Expenses and Line Items

The financial model centres on two tables:

```text
budget.expenses
budget.expense_items
```

`budget.expenses` represents one canonical purchase. `budget.expense_items` contains the individual products or line items belonging to that purchase.

Examples of information retained on the canonical expense include:

- source and source order ID
- merchant/store
- transaction date and receipt-local transaction time
- account/payment reference
- receipt totals, taxes, discounts, fees, and tips
- extraction confidence/status
- reconciliation difference
- raw source payload

Each line item can retain quantity, weight, unit cost, line total, tax code, and its categorization audit fields.

### Receipt reconciliation

The schema exposes a reconciliation value that compares the receipt components with the canonical expense total. Conceptually:

```text
reconciliation difference
    = item subtotal
    - discounts
    + tip
    + service fee
    + recycling fee
    + fee tax
    + GST
    + PST
    - expense total
```

A value near zero indicates that the extracted components reconcile with the purchase total.

---

## 5. Receipt Item Categorization

Categorization is performed at **line-item level**, not simply at merchant level. This is important for merchants such as Costco, where a single transaction can contain groceries, health products, clothing, household supplies, and other categories.

The canonical categorization pipeline is implemented by `CategoryMappingPipeline`.

### Categorization order

For each canonical line item, Ledger follows this order:

1. Normalize the product description.
2. Apply verified deterministic product overrides and configured rules.
3. Reuse previously approved product mappings.
4. Apply deterministic keyword and fuzzy-keyword rules.
5. Use an optional AI fallback for an unknown or ambiguous item.
6. Route unresolved or low-confidence results to human review.

This ordering deliberately prefers repeatable deterministic behaviour over AI calls.

### Category decision audit trail

A category assignment is not stored as only a category name. Each `budget.expense_items` row can preserve:

```text
budget_category
category_source
category_confidence
category_rationale
category_requires_review
categorized_at
```

`category_source` records provenance using values such as:

```text
rule
learned_mapping
ai
manual
unresolved
```

This makes it possible to distinguish a deterministic rule from a human correction or an AI-generated suggestion when reviewing historical data.

### Deterministic mappings

Known products can be categorized using deterministic rules and verified product overrides. The same normalized product description therefore produces the same category without requiring an AI call.

Configured product rules can additionally be stored in `budget.expense_category_mappings` and scoped by source or merchant when necessary.

### Learned mappings and human corrections

A human-approved correction can become an approved reusable mapping.

For example:

```text
unknown product
    -> human selects Indoor Supplies
    -> approved exact mapping is saved
    -> next occurrence resolves as learned_mapping
```

This allows the system to improve through use while keeping the learned decision explicit and auditable.

### AI fallback

The category pipeline supports an AI fallback after deterministic and learned mappings have failed. AI is therefore used as an exception path rather than the default classifier.

The AI classification contract includes a category and confidence value. Category decisions also support rationale and a review flag so low-confidence model output can be reviewed before being treated as final.

The AI engine constrains model output to the configured category catalogue rather than allowing arbitrary new category names.

### Human review

An item that cannot be confidently mapped is not silently changed to `Groceries` or another default category. It remains unresolved and is marked:

```text
category_requires_review = true
```

This is important because an incorrect deterministic default would otherwise contaminate budget reporting and future learned mappings.

---

## 6. Category Catalogue and Groups

The budget taxonomy is configuration data rather than application code.

The authoritative catalogue is:

```text
config/categories.yaml
```

The YAML file defines:

- category groups
- category names
- descriptions
- display/sort order
- aliases for renamed or imported categories

The current top-level groups are:

```text
Mandatory
Sinking
Discretionary
Investments
```

Examples:

```text
Mandatory      -> Groceries, Hydro, Fuel, Home Mortgage
Sinking        -> Autopac, Home Insurance, Vacation, Christmas
Discretionary  -> Dining, Gifts, Fun / Entertainment
Investments    -> RRSP, TFSA, Mortgage PrePayment
```

Legacy category names can be retained as aliases without making them active categories. Examples include:

```text
Online Svcs             -> Subscriptions
Birthday / Celebrations -> Gifts
Misc Paul & Rox          -> Misc Household
```

This allows historical imports to normalize cleanly while the current catalogue remains generic and consistent.

### PostgreSQL catalogue tables

The YAML catalogue is loaded into:

```text
budget.category_groups
budget.expense_categories
budget.expense_category_aliases
```

The application runtime uses the same YAML catalogue as the authoritative list of allowed categories, preventing the Python classifier and PostgreSQL catalogue from drifting apart.

---

## 7. Category Splits

After every line item belonging to a canonical purchase has been categorized, Ledger aggregates the item amounts by category through:

```text
budget.expense_category_splits
```

A split contains:

```text
expense_pk
category_name
category_amount
item_count
requires_review
```

For example, a single $250 Costco transaction could become:

```text
Groceries          $168.42
Indoor Supplies     $31.99
Health & Fitness    $44.99
Clothing             $4.60
```

This is the key capability that allows Ledger to avoid assigning an entire mixed-merchant transaction to one budget category.

These splits are also the natural input to a future Vertex42 Money Manager export.

---

## 8. Accounts and Payment Sources

Accounts are deliberately separate from expense categories.

The account model uses:

```text
budget.accounts
budget.account_balances
budget.payment_cards
```

Typical account types include:

```text
chequing
savings
investment
prepaid
credit_card
cash
other
```

An account can have a target balance (`goal_amount`). Historical/current balances are stored separately so balances can be tracked over time.

The goal percentage is derived rather than stored:

```text
goal percentage = cleared balance / goal amount * 100
```

Credit cards, prepaid accounts, and bank accounts therefore remain payment sources and never become budget expense categories.

---

## 9. PostgreSQL Bootstrap

`sql/schema_phase1.sql` is the canonical core DDL for a fresh Ledger database.

Initialize the core runtime schema or apply its additive upgrades to an existing
database with:

```bash
ledger database setup
```

Receipt OCR runs and their individual passes are retained in
`budget.receipt_ocr_runs` and `budget.receipt_ocr_passes`. Record verified
ground truth after reviewing a receipt with:

```bash
ledger receipts feedback 123 --outcome confirmed
ledger receipts feedback 123 --outcome corrected \
  --corrected-fields '{"merchant":"Sobeys","total":"42.17"}'
```

Use `budget.receipt_ocr_effectiveness` to compare selection frequency, runtime,
consensus coverage, and verified outcomes by merchant, file type, page, and OCR
configuration.

The `/sql` directory intentionally separates schema from reporting queries.

Household category configuration is **not** embedded in the SQL schema. On a
fresh Kubernetes PostgreSQL volume, `scripts/stage_db_bootstrap.sh` stages the
canonical schema, category catalogue, analytics views, financial transactions, receipt
deduplication, constraints, receipt processing, merchant aliases, and product
enrichment DDL.

The staged bootstrap represents the desired state for a clean deployment;
additive runtime migrations upgrade existing databases.

---

## 10. Running Categorization Against PostgreSQL

The project exposes a command for categorizing all line items belonging to one canonical expense:

```bash
home-budget-categorize <expense_pk>
```

The command requires a PostgreSQL connection through `HOME_BUDGET_PG_DSN`,
`DATABASE_URL`, `--pg-dsn`, or `--database-url`.

Example:

```bash
export DATABASE_URL='postgresql://home_budget:...@localhost:5432/home_budget'
home-budget-categorize 123 --source costco --merchant Costco
```

Avoid placing database passwords directly in shell history where possible.

---

## 11. Ledger Web Application

The authenticated Ledger web UI is a receipt-first workspace for inspecting
source documents, OCR results, canonical expenses, reconciliation, duplicates,
data-quality findings, and processing status. It also supports audited category
overrides, category-rule management, and item description or product-link
corrections.

The main browser pages include:

```text
/ledger/receipts
/ledger/receipts/<source_sha256>
/ledger/dashboard
/ledger/expenses
/ledger/category-spend
/ledger/category-rules
/ledger/review-queue
/ledger/extraction-audit
/ledger/duplicates
/ledger/transactions
/ledger/receipt-processing
/ledger/expenses/<expense_pk>
/ledger/evidence/<evidence_id>
```

Receipt evidence pages include a **View receipt** action. PDFs and image files are served inline only when requested. The application resolves `receipt_evidence.source_reference` beneath `RECEIPT_SOURCE_ROOT` and rejects paths that escape that configured root.

### Run locally

Install the database extra and set the PostgreSQL URL:

```bash
python -m pip install -e '.[db]'
export DATABASE_URL='postgresql://home_budget:...@localhost:5432/home_budget'
export LEDGER_BASE_PATH='/ledger'
export RECEIPT_SOURCE_ROOT='/path/to/receipts/raw/scanned/inbox'
home-budget-ledger
```

The browser application normally sits behind oauth2-proxy, which supplies authenticated identity headers. Health endpoints do not require those identity headers:

```text
/ledger/health   liveness
/ledger/ready    database-backed readiness
```

Read paths execute `SET TRANSACTION READ ONLY`. The three supported write paths
use explicit transactions and record the authenticated user in audit tables:
category overrides, category rules, and item description/product-link changes.

### Kubernetes / K3s

`k8s/ledger-web-service.yaml` deploys the web service as a ClusterIP application. It receives `DATABASE_URL` from `postgres-secret`, sets `RECEIPT_SOURCE_ROOT=/data/receipts/raw/scanned/inbox`, and mounts the existing `home-budget-data` PVC at `/data` **read-only**.

The deployment probes are:

```text
liveness  -> /ledger/health
readiness -> /ledger/ready
```

The existing ingress and oauth2-proxy resources continue to provide Microsoft Entra ID browser authentication before traffic reaches the Ledger service.

---

## 12. Kubernetes and GitOps Deployment

Ledger is deployed to K3s using Argo CD. See the
[solution architecture](docs/solution-architecture.md) for the complete runtime
and delivery diagrams.

The base deployment includes:

```text
Internet / LAN
    -> reverse proxy / TLS
    -> K3s ingress
    -> oauth2-proxy
    -> Microsoft Entra ID authentication
    -> Ledger web application

K3s
    -> Ledger web
    -> oauth2-proxy
    -> PostgreSQL StatefulSet + persistent volume
    -> receipt processor CronJob
    -> product enrichment CronJob template
    -> shared receipt data and OCR cache PVC
```

The optional `deploy/rabbitmq` overlay changes the receipt CronJob to a
publisher and adds RabbitMQ plus a receipt-worker Deployment. Private-LAN
variants are documented in the [network access guide](docs/private-networking.md).

A fresh PostgreSQL persistent volume automatically receives the staged schema,
category catalogue, analytics views, and supporting processing tables during
initialization.

The PostgreSQL persistent volume is intentionally independent from Argo application synchronization. Running:

```bash
argocd login argocd.brownrook.net --grpc-web
argocd app sync ledger --grpc-web
argocd app wait ledger --sync --health --timeout 600 --grpc-web
make status
```

updates the desired Kubernetes resources but does not by itself erase an existing PostgreSQL data volume.
See the [operations runbook](docs/operations-runbook.md#argo-cd-deployment) for
browser and CLI login, status prerequisites, synchronization, and recovery.

---

## 13. Tests

Use the Make targets so unit and integration tests receive the repository's
standard configuration:

```bash
make test       # unit tests
make test-db    # integration tests; resets the disposable test schemas
make test-rabbit # named RabbitMQ integration tests
make test-all   # unit and integration tests
```

Run `make help` to list development, test, and operational targets. See the
[operations runbook](docs/operations-runbook.md#make-targets) for details.

Build or preview the online manual locally after installing the documentation
extra with `python -m pip install -e '.[docs]'`:

```bash
make docs-build
make docs-serve
```

The test suite covers receipt ingestion/OCR, canonical expense construction,
categorization, SQL analytics contracts, transaction reconciliation, receipt
deduplication, authenticated web queries, and audited correction workflows.

---

## 14. Design Boundaries

The data flow is:

```text
receipt ingestion
    -> canonical purchase model
    -> reliable item categorization
    -> human review / learned mappings
    -> category splits
    -> account transaction matching
    -> budget reporting and export
    -> Ledger web UI
```

Ledger provides receipt-first inspection with narrowly scoped, authenticated,
and audited correction workflows. Canonical expenses remain the financial
record, receipt artifacts remain evidence, and payment accounts remain separate
from budget categories.

---

## 15. Local Entra-authenticated Ledger

The development proxy runs Caddy and OAuth2 Proxy in Docker while Ledger runs
directly from the working tree. This avoids the image build and K3S deployment
loop for application changes.

1. Copy `.env.dev.example` to `.env.dev`, fill in the Entra and database values,
   and generate the separate `LEDGER_PROXY_SECRET` described in that file.
2. Add `127.0.0.1 ledger-dev.brownrook.net` to `/etc/hosts`.
3. Add `https://ledger-dev.brownrook.net/ledger/oauth2/callback` as a redirect URI in Entra.
4. Start PostgreSQL forwarding with `kubectl -n home-budget port-forward svc/postgres 5433:5432`.
5. Run `make dev-up`, then `make dev-cert-install` once to trust Caddy's local CA.
6. Run `make dev-web` and open `https://ledger-dev.brownrook.net/ledger`.

Stop the proxy with `make dev-down`. The local Ledger process is intentionally
outside Docker so source changes only require restarting that process.
Caddy publishes ports 80 and 443 only on `127.0.0.1`. Ledger listens on port
8080 for the Docker proxy, and verifies `LEDGER_PROXY_SECRET` before trusting
the proxy's identity headers. See the [network access guide](docs/private-networking.md)
for the local and K3s boundaries.

To discard and rebuild the local `home_budget` schemas with the exact SQL
bootstrap sequence used by K3S, run `make dev-db-reset`. The command refuses
to run unless PostgreSQL reports the local `home_budget` database.
