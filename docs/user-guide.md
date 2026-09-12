# BrownRook Ledger user guide

BrownRook Ledger is an authenticated, receipt-first workspace for reviewing
canonical household expenses, their line items, and the evidence used to create
them. Sign in through the normal Ledger URL and start from **Receipts** or the
accounting and review dashboard.

## Recommended review order

### 1. Receipts

**Receipts** is the default landing page. It lists each discovered source file
with its merchant, transaction time, total, processing state, and extraction
state. Open a receipt to review the source document beside extracted text,
canonical line items, and accepted product-enrichment evidence. Use the link to
the accounting and review dashboard for cross-receipt reports and corrections.

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
  exhausted, after which an operator can reset it with `brownrook receipts retry`
  once the underlying problem is fixed.

Write controls are limited to category overrides, category-rule management, and
item description/product-link corrections. Read queries use read-only database
transactions; supported changes use explicit transactions and retain the
authenticated actor in audit records.
