# BrownRook Ledger / Home Budget Pipeline

BrownRook Ledger is a receipt and household-expense processing pipeline. Its purpose is to turn receipt evidence from multiple sources into a canonical set of purchases and line items that can be categorized, reviewed, aggregated, and eventually exported to a budgeting system such as Vertex42 Money Manager.

The design separates four different concerns:

```text
Receipt evidence  ->  Canonical expense  ->  Categorization  ->  Budget reporting
PDF / scan / web      purchase + items       category/item       category splits
```

A payment account answers **where the money came from**. A budget category answers **what the money was spent on**. Ledger deliberately keeps those concepts separate.

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

Ledger currently supports receipt data originating from:

- Instacart electronic receipts
- Costco receipts
- Sobeys receipts
- scanned paper receipts

The canonical source vocabulary is stored on `budget.expenses.source`.

### Scanned paper receipts

Scanned receipts can be discovered and processed without immediately modifying PostgreSQL. A dry-run is useful for validating OCR quality and reconciliation before ingesting data.

```bash
.venv/bin/python3 scanned_receipt_ingest.py /path/to/scanned-receipts \
  --json scanned_receipts_report.json
```

Receipts with missing totals, missing items, or low extraction confidence can be marked for review. Scans for which usable OCR text cannot be extracted can be marked unreadable.

To persist the result:

```bash
.venv/bin/python3 scanned_receipt_ingest.py /path/to/scanned-receipts \
  --write-db \
  --db-dsn "host=localhost port=5432 dbname=home_budget user=postgres"
```

Scanned input is idempotent. The SHA-256 hash of the source artifact is retained so rerunning the same receipt does not create an unrelated duplicate purchase.

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
    + discounts
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

`sql/schema_phase1.sql` is the canonical bootstrap DDL for a fresh Ledger database.

The `/sql` directory intentionally separates schema from reporting queries:

```text
sql/
├── schema_phase1.sql
└── queries/
    ├── instacart_items.sql
    └── instacart_summary.sql
```

Household category configuration is **not** embedded in the SQL schema. On a fresh Kubernetes PostgreSQL volume, bootstrap runs in two stages:

```text
001-schema.sql
    -> creates the database structure

config/categories.yaml
    -> category catalogue renderer
    -> 002-categories.sql
    -> loads groups, categories, and aliases
```

During the current pre-production phase, `schema_phase1.sql` represents the desired clean database rather than a long sequence of historical story-specific migrations.

---

## 10. Running Categorization Against PostgreSQL

The project exposes a command for categorizing all line items belonging to one canonical expense:

```bash
home-budget-categorize <expense_pk>
```

The command requires a PostgreSQL connection through `DATABASE_URL` or `--database-url`.

Example:

```bash
export DATABASE_URL='postgresql://home_budget:...@localhost:5432/home_budget'
home-budget-categorize 123 --source costco --merchant Costco
```

The command prints the number of categorized items, whether review is required, and the resulting category splits.

Avoid placing database passwords directly in shell history where possible.

For standard PostgreSQL tooling, `~/.pgpass` is recommended:

```bash
cat > ~/.pgpass <<'EOF'
localhost:5432:home_budget:home_budget:YOUR_PASSWORD
EOF
chmod 600 ~/.pgpass
```

---

## 11. Kubernetes and GitOps Deployment

Ledger is deployed to K3s using Argo CD.

The current deployment includes:

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
```

A fresh PostgreSQL persistent volume automatically receives the canonical schema and category catalogue during initialization.

The PostgreSQL persistent volume is intentionally independent from Argo application synchronization. Running:

```bash
argocd app sync ledger --grpc-web
```

updates the desired Kubernetes resources but does not by itself erase an existing PostgreSQL data volume.

---

## 12. Tests

Run the project tests with:

```bash
python -m pytest
```

Focused categorization tests include:

```text
tests/test_category_catalog.py
tests/test_category_mapping_pipeline.py
tests/test_canonical_expense_categorization.py
```

These tests cover catalogue grouping and aliases, deterministic mappings, learned mappings, AI decision handling, unresolved review behaviour, persistence orchestration, and category split aggregation.

Receipt-focused tests cover OCR-derived metadata, receipt evidence, scanned receipt ingestion, receipt annotation handling, payment extraction, date/time extraction, and reconciliation.

---

## 13. Current Design Direction

The intended progression is:

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

The immediate goal of the categorization work is to ensure that one canonical purchase is categorized once at line-item level, with enough provenance and review information to trust the resulting budget splits.
