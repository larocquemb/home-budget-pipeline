# Bank transactions and receipt cross-reference

Ledger keeps bank transactions separate from receipt extraction. A bank date or
merchant description is corroborating evidence; confirming an allocation does
not rewrite OCR, receipt fields, or receipt line items.

## Durable storage and account ownership

Development accounting uses **home_budget_accounting_dev** on Arsene port 15432,
in the **accounting** schema. Receipt processing uses **home_budget_dev**. Set
`ACCOUNTING_DATABASE_URL` separately from `DATABASE_URL`; transaction operations
refuse to fall back to receipt storage. Provision the dedicated database/role
with `make dev-accounting-setup`. This preserves the receipt database.

The **Accounts** page shows these records:

| Table | Purpose |
| --- | --- |
| `accounting.accounts` | Institution, stable account reference and primary card ending |
| `accounting.account_numbers` | Linked identifiers containing only the last four digits |
| `accounting.account_ownerships` | Account owners and cardholders, roles, assertion date and optional effective dates |
| `accounting.financial_transactions` | Normalized bank rows |
| `accounting.transaction_imports`, `accounting.transaction_import_rows` | Import and original-row provenance |
| `accounting.source_artifacts` | Verified source archive paths and hashes |
| `accounting.transaction_allocations`, `accounting.transaction_allocation_audit` | Splits, receipt source links and correction history |

No full account/card number is accepted as an account identifier. Card input in
the CSV must already be masked. A shared account's primary holder is separate
from each linked cardholder. Current ownership assertions do not assign a payer
to historical transactions; missing historical effective dates remain unknown.

Record confirmed ownership with a private JSON plan:

```json
{
  "reference": "Example card account",
  "primary_last4": "1234",
  "primary_owner": "Primary holder",
  "cardholders": {"1234": "Primary holder", "5678": "Additional holder"},
  "archive_root": "/private/transactions/raw/institution/card/x1234"
}
```

```sh
ledger --env-file .env.dev transactions ownership --plan /private/ownership.json
ledger --env-file .env.dev transactions ownership --plan /private/ownership.json \
  --apply --actor 'Paul La Rocque'
```

For the legacy bank tables originally stored with receipts, first provision the
accounting database, then preview and apply `ledger --env-file .env.dev transactions
migrate-receipt-store --apply --actor 'Paul La Rocque'` (omit `--apply` to preview).
Migration verifies every copied row and leaves the legacy originals intact.
After switching, new reads/writes use only the accounting database. Retired bank
tables in receipt bootstrap are compatibility scaffolding, not active storage.

Receipt database rebuilds leave accounting records intact. Confirmed receipt
links use immutable source hashes; regenerated expense integers are display hints
and cannot silently relink an allocation to a different receipt. A source not yet
reimported remains a saved link rather than becoming a different expense.
Destroying the shared PostgreSQL volume would still affect both databases;
separate databases do not replace independent accounting backups.

## Import CIBC MasterCard CSV

Use the original five-column export without adding a header:
date, description, debit, credit, masked card number. The importer validates every
row before writing. It accepts CAD records, stores purchases as positive amounts,
and stores credits and card payments as negative amounts. Payment descriptions
containing `PAYMENT` or `PAIEMENT` identify card-balance transfers; other credits
remain credits, without assuming that they are refunds.

The file has one date column. Ledger retains that bank-export date and leaves
posting date unknown; it does not claim the date is the original purchase date.
Card ownership and payer are not inferred from a masked card number.

```sh
ledger --env-file .env.dev transactions setup --apply --actor 'Paul La Rocque'
ledger --env-file .env.dev transactions import-cibc /private/path/export.csv \
  --account-reference 'CIBC MasterCard'
ledger --env-file .env.dev transactions import-cibc /private/path/export.csv \
  --account-reference 'CIBC MasterCard' --apply --actor 'Paul La Rocque'
```

Preview is the default. Setup adds tables without rebuilding existing data.
Imports retain the source hash, filename, actor, import timestamp, each original
row, and its relationship to the normalized transaction. Keep the original CSV
in private backed-up storage; database provenance does not establish that a
separate backup exists. Raw bank data must stay out of public Git and Jira.

Reuse the same account reference for subsequent exports. Fingerprints use the
account reference, card ending, date, normalized description, signed amount and
identical-occurrence number. Reordering a file or changing its filename does not
create duplicates, and two identical purchases in one export remain two records.

Without bank transaction IDs, a partial export containing only one of two
otherwise identical purchases cannot identify which occurrence it represents.
Use complete date ranges that include all same-day identical entries. Corrections
to dates/descriptions and independently imported partial exports require review;
fingerprints are not proof that two records represent the same purchase.

## Review receipt candidates

Open **Transactions**, then select a transaction. Candidates have an exact amount
and receipt dates within seven days; conflicting known card endings exclude a
candidate. Suggestions may include unrelated merchants with the same amount.
Check the merchant, card, original document and date before saving a link. The
date-source column distinguishes receipt extraction from a legacy expense date
that may have come from a filename and needs source verification. Missing
receipt dates and negative bank entries do not receive automatic candidates.

For a private read-only report:

```sh
ledger --env-file .env.dev transactions suggest
ledger --env-file .env.dev transactions suggest --transaction-id 123
```

No suggestions automatically confirm matches, change receipt statuses or fill
store/date fields. Confirmed receipt links are recorded as allocations.

## Record a split

On the transaction page, enter a category and optional person, expense ID and
note. Use **Add split** for additional rows. The displayed remainder must reach
zero. Add a reason, then select **Save confirmed allocations**. One allocation
with an expense ID records a whole-transaction receipt link. Several allocations
can link different expenses, or use different categories/people for one purchase.
These allocations do not change receipt item categories or existing category
spend reports; they are a separate bank-allocation record.

Each allocation must have the bank amount's sign, use whole cents, and all rows
must sum exactly to the transaction amount. Card payments are transfers and
cannot be allocated to expenses. Negative credits can be allocated explicitly.
The system retains the actor, reason, previous allocations and new allocations
for each revision. Stale edits are rejected; reload before editing again.

The same operation is available with a private JSON plan:

```json
{
  "expected_version": 0,
  "reason": "Reviewed the receipt and household split",
  "allocations": [
    {"amount": "25.00", "category": "Groceries", "person": "Paul"},
    {"amount": "15.00", "category": "Household", "expense_pk": 123}
  ]
}
```

```sh
ledger --env-file .env.dev transactions allocate 123 --plan /private/path/split.json
ledger --env-file .env.dev transactions allocate 123 --plan /private/path/split.json \
  --apply --actor 'Paul La Rocque'
```

Version zero means no allocation has been saved yet. Existing allocations and
their version are visible in the transaction page and allocation history. A bank
transaction remains one record; splitting it does not create duplicate spending.
