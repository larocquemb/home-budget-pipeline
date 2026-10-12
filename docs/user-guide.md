# BrownRook Ledger user guide

BrownRook Ledger is an authenticated, receipt-first workspace for reviewing
canonical household expenses, their line items, and the evidence used to create
them. Sign in through the normal Ledger URL and start from **Receipts** or the
accounting and review dashboard.

## Recommended review order

### 1. Receipts

**Receipts** is the default landing page. It lists each discovered source file
with its merchant, transaction time, total, processing state, and extraction
state. The **Expense ID** column links to the associated accounting record;
unprocessed receipts show **Pending**. Use **Search receipts** to search across
all pages by expense ID, filename, merchant, date, total, or item description.
Multiple words can match different fields, for example `Sobeys 2026-02-14`.
Click any column heading to toggle ascending **▲** and descending **▼** sorting.
The current search and sort are preserved when navigating pages; **Clear** resets
them. Expense IDs and totals sort numerically, and missing values remain last.

Receipt links and page titles lead with the expense number, merchant, date, and
total when available. The source filename remains visible. Expense IDs are local
to each database, so identify the same purchase across environments by filename,
merchant, date, and total rather than expecting matching IDs. The receipt URL
uses its stable source-file hash; `/expenses/ID` redirects to the associated receipt.

Open a receipt to review the source document beside extracted text,
canonical line items, and accepted product-enrichment evidence. Use the link to
the accounting and review dashboard for cross-receipt reports and corrections.

#### Understand item readings and product recommendations

The **Receipt item** column shows the imported item text until a correction is
accepted. When an accepted recommendation includes a supported reading correction,
the row leads with the **Accepted interpretation**. Expand **Import history** to
see the text selected during import.

For example, an item imported as `Cep Pic Med` can display the accepted
interpretation `Oep Pic Med`, with **Old El Paso Salsa Picante Style Restaurant
Medium 650 ml** in the Description column. `Cep` remains in import history;
it does not mean every OCR pass read `Cep`. The interpretation comes from the
recorded product evidence and acceptance, rather than changing an OCR observation.

Expand **Related OCR observations** to compare readings such as `Cep` and `Oep`.
Identical observed text is displayed once per reading. Expand **Source records (N)**
to inspect the retained observations with engine, pass ID, variant, OCR run UUID,
page and OCR output line when available, plus observation and receipt evidence IDs.
**Combined OCR text** comes from the stored consensus and may have no recorded run
UUID. **OCR pass** identifies a specific engine pass. Several passes can share one
run UUID, and one pass can contain several different lines; the observation ID
distinguishes the individual source records.

**Row association unverified** means a related observation may describe a
neighbouring or duplicate receipt item. An OCR output line number is a position
in that pass's text, not a Ledger item number. Repeated readings are useful
evidence, but their count alone does not establish the correct reading or row.

The **Enrichment** column separates a saved recommendation from an accepted one.
Review the product link, **Recommendation evidence**, and **Source webpage title**
before choosing **Accept recommendation for this item**. Product labels omit
shopping prompts such as “Buy”; the original search-result title stays in the
evidence. An evidence score is a matching score, not a guarantee of correctness.

Acceptance saves the description and product link for the selected item and
records an audit event. It also applies supported category rules to an unresolved
category, so an accepted salsa product can become **Groceries**. Existing categories
and human overrides are preserved. The imported item text and amount remain intact.
If the item or saved
recommendation changes during review, reload the receipt before accepting again.

Use **Apply accepted matches and category rules** above the receipt's line items
to repair older accepted rows and fill empty product fields from previously
accepted matches for the same retailer and normalized item text. Cached matches
are rechecked against product-page and token evidence. This explicit receipt-wide
action can fill an identical second row without another model call; it preserves
existing descriptions, categories and per-item collaboration reviews, and records
changes in the audit history. It does not accept a new model recommendation.

**Awaiting product identification** means there is no accepted description for that
item yet. A one-item collaboration does not enrich the whole receipt. Ask the
operator to process the remaining items; unmatched products stay unresolved.
**Not enriched** means no enrichment result has been saved. Neither label reports
OCR progress: product enrichment is a separate command after receipt import.

Check **Processing** in the receipt list or detail page and refresh to see changes.
**Active** means work is in progress; **Succeeded** means the result was persisted.
**Review required** indicates processing completed with a review concern. Ledger
does not yet show live logs or a progress bar. For local background processing,
use `make dev-receipts-logs`; see the [local development guide](local-development.md).

#### Follow the processing story

Use **Processing graph** beside a receipt in the list or **View receipt knowledge
graph** on its detail page. The **Receipt graph** navigation link opens the general
graph browser.

The processing story follows a selected item and collaboration run through:

1. OCR and receipt evidence;
2. model expansion of abbreviated or ambiguous text;
3. Brave search results;
4. model verification against the recorded evidence;
5. the final recommendation and any acceptance record.

Select a card to inspect its evidence and links. Compare other attempts and runs
to understand how the result changed. A model recommendation and human acceptance
are separate steps; the graph shows recorded evidence and decisions, not every
possible search or interpretation. See the [receipt knowledge graph guide](receipt-knowledge-graph.md)
for projection, provenance, and operational details.

### 2. Extraction audit

Open **Extraction audit** first. The report defaults to receipts with four or
fewer canonical line items, which are the most likely to have incomplete OCR
or parsing.

For each row:

1. Open **Evidence** and compare the cropped receipt image with the extracted
   text. PDF evidence is rendered as an image with receipt boundaries detected;
   sparse scanner noise and the surrounding page margins are excluded.
   Move the pointer over the image for a steady 1.5× magnification of the area
   beneath it. On wider screens, extracted text appears beside the receipt so
   both can be reviewed together; smaller screens stack them vertically.
   Use **Open original receipt** when the full PDF viewer is more convenient.
2. Compare the scan with its extracted text.
3. Open **Expense** and compare merchant, date, total, and line items.
4. Treat `unreadable` as needing new evidence or better OCR.
5. Treat `review` as needing a human check before relying on the result.
6. Investigate `complete` receipts with unexpectedly few items; `complete`
   means required fields were found, not that every line was recognized.

Raise **Maximum items** to broaden the audit after the lowest-count receipts
have been checked.

### 3. Review queue

Open **Review queue** for canonical expenses with extraction, reconciliation,
or data-quality concerns. Use the expense link to inspect its items and linked
receipt evidence.

### 4. Expenses

**Expenses** lists canonical purchases, not individual items. Filter by source,
merchant, or review state. Opening an expense shows:

- canonical merchant, date, account, and total;
- canonical line items and category decisions;
- linked receipt evidence and the original document.

From an expense, you can save an additional item description or merchant
product URL. You can also save a category override. An override creates or
updates an approved exact-match rule for the same source and merchant, applies
it to matching imported items, and records the authenticated user in the audit
history.

### 5. Category rules

Use **Category rules** to create, change, disable, and search approved matching
rules. A rule can be scoped by source and merchant, use exact or contains
matching, and set its priority. Saving an active rule immediately applies it to
matching imported items and records the change in the rule audit history.

### 6. Receipt processing

Use **Receipt processing** to answer whether a source file was discovered,
attempted, completed, marked for review, or failed. A successful processor run
may still contain `review_required` receipts.

To rerun extraction for one completed receipt, an operator can use
`ledger receipts reprocess SOURCE_REFERENCE --verbose`, with the receipt's exact
path relative to the inbox. This queues a RabbitMQ request and returns `queued`;
the consumer refreshes OCR and replaces its extraction results. See
[receipt processing](receipt-processing.md) for commands and progress monitoring.

### 7. Duplicates

Use **Duplicates** to inspect evidence records that may describe the same
purchase. Do not infer that two similar scans are separate purchases until the
canonical expense links and receipt details have been checked.

### 8. Category spend and transactions

Use **Category spend** after line-item categorization is sufficiently complete.
Use **Transactions** to inspect matches between canonical expenses and imported
financial transactions.

## Status meanings

- `complete`: required extraction fields passed current checks.
- `review`: extraction completed but needs human verification.
- `unreadable`: useful receipt data could not be extracted.
- `succeeded`: backlog processing completed without an application error.
- `review_required`: processing completed, but the receipt needs review.
- `failed`: processing raised an error; it retries until its attempt budget is
  exhausted, after which an operator can reset it with `ledger receipts retry`
  once the underlying problem is fixed.

Write controls are limited to category overrides, category-rule management, and
item description/product-link corrections, including explicit recommendation
acceptance. Read queries use read-only database
transactions; supported changes use explicit transactions and retain the
authenticated actor in audit records.
