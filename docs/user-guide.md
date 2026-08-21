# BrownRook Ledger user guide

BrownRook Ledger is a read-only view of canonical household expenses, their
line items, and the receipt evidence used to create them. Sign in through the
normal Ledger URL and start from the dashboard.

## Recommended review order

### 1. Extraction audit

Open **Extraction audit** first. The report defaults to receipts with four or
fewer canonical line items, which are the most likely to have incomplete OCR
or parsing.

For each row:

1. Open **Evidence** and compare the embedded receipt preview with the
   extracted text. Use **Open original receipt** when a separate viewer is
   more convenient.
2. Compare the scan with its extracted text.
3. Open **Expense** and compare merchant, date, total, and line items.
4. Treat `unreadable` as needing new evidence or better OCR.
5. Treat `review` as needing a human check before relying on the result.
6. Investigate `complete` receipts with unexpectedly few items; `complete`
   means required fields were found, not that every line was recognized.

Raise **Maximum items** to broaden the audit after the lowest-count receipts
have been checked.

### 2. Review queue

Open **Review queue** for canonical expenses with extraction, reconciliation,
or data-quality concerns. Use the expense link to inspect its items and linked
receipt evidence.

### 3. Expenses

**Expenses** lists canonical purchases, not individual items. Filter by source,
merchant, or review state. Opening an expense shows:

- canonical merchant, date, account, and total;
- canonical line items and category decisions;
- linked receipt evidence and the original document.

### 4. Receipt processing

Use **Receipt processing** to answer whether a source file was discovered,
attempted, completed, marked for review, or failed. A successful processor run
may still contain `review_required` receipts.

### 5. Duplicates

Use **Duplicates** to inspect evidence records that may describe the same
purchase. Do not infer that two similar scans are separate purchases until the
canonical expense links and receipt details have been checked.

### 6. Category spend and transactions

Use **Category spend** after line-item categorization is sufficiently complete.
Use **Transactions** to inspect matches between canonical expenses and imported
financial transactions.

## Status meanings

- `complete`: required extraction fields passed current checks.
- `review`: extraction completed but needs human verification.
- `unreadable`: useful receipt data could not be extracted.
- `succeeded`: backlog processing completed without an application error.
- `review_required`: processing completed, but the receipt needs review.
- `failed`: processing raised an error and remains eligible for retry.

The web application is currently read-only. Corrections and category changes
must use the supported command-line/database workflows until write-capable
review controls are added.
