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
| `accounting.accounts` | Institution, nickname, stable account reference and primary card ending |
| `accounting.account_profile_audit` | Nickname/profile changes without changing import identity |
| `accounting.account_numbers` | Linked identifiers containing only the last four digits |
| `accounting.account_ownerships` | One current ownership record per card ending, with primary/secondary role |
| `accounting.account_ownership_audit` | Original and corrected ownership snapshots, actor, reason and timestamp |
| `accounting.financial_transactions` | Normalized bank rows |
| `accounting.transaction_imports`, `accounting.transaction_import_rows` | Import and original-row provenance |
| `accounting.source_artifacts` | Verified source archive paths and hashes |
| `accounting.transaction_allocations`, `accounting.transaction_allocation_audit` | Splits, receipt source links and correction history |

No full account/card number is accepted as an account identifier. Card input in
the CSV must already be masked. Each linked card has one ownership record: the
primary card identifies the primary holder, and additional cards are secondary.
There is no extra account-level ownership row. Current ownership assertions do not assign a payer
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

## Import a CIBC archive folder

`import-tree` supports both five-column card exports and four-column deposit
exports (date, description, withdrawal, deposit). It recognizes `mastercard` and
`visa` as credit cards, `smart` as chequing, and `eAdvantage` as savings. Use the
last four digits in an `x1234` account folder; a year folder below it is optional.
For example: `smart/x1234/2026/2026-11-03.csv`.

Preview all files first:

```sh
ledger --env-file .env.dev transactions import-tree /private/transactions/raw/cibc
```

Then import them:

```sh
ledger --env-file .env.dev transactions import-tree /private/transactions/raw/cibc \
  --apply --actor 'Paul La Rocque'
```

The command validates all files before writing and applies the folder in one
database transaction. Preview totals account for overlapping files within the
folder as well as records already imported. Existing primary-card account labels
are reused to preserve fingerprints. New accounts get a product/last-four label;
unknown owners stay unassigned. Repeating the command imports only new records.

### USD accounts

New accounts default to CAD. Declare a USD account explicitly on its first import:

```sh
ledger --env-file .env.dev transactions import-tree /private/transactions/raw/cibc \
  --account-currency visa/x1234=USD
```

Review the per-file currency in the preview, then add `--apply --actor 'Paul La Rocque'`.
The account retains its registered currency for subsequent folder imports; a
conflicting override is rejected. For a single file, use `--currency USD` with
`import-cibc`. Original USD amounts are retained without conversion. Folder totals
count rows and do not add CAD and USD monetary amounts together.

Receipt-currency verification currently supports CAD; USD transactions do not
receive CAD receipt suggestions or allow unverified cross-currency receipt links.
USD category/person allocations can be saved in USD. CAD reporting would need a
separate converted amount, exchange rate, rate date and source; no conversion or
exchange rate is currently inferred from an export.

Deposit-account withdrawals are positive and deposits negative, following the
same outflow/inflow sign convention as the bank transaction ledger. These generic
types do not assume whether a row is spending, income, a refund or a transfer.
No payment-card number is invented for a deposit account. Transaction exports
alone do not establish an opening balance or current bank balance.

A single deposit-account file can also be imported explicitly:

```sh
ledger --env-file .env.dev transactions import-cibc /private/export.csv \
  --account-reference 'CIBC Smart x1234' --account-type chequing --account-last4 1234
```

Add `--apply --actor 'Paul La Rocque'` after reviewing its preview. Use `savings`
for a savings account. Do not pass the full bank account number.


## Combined institution archives and nicknames

Point `import-tree` at the shared `raw` folder to include both `acu` and `cibc`:

```sh
ledger --env-file .env.dev transactions import-tree /private/transactions/raw \
  --account-currency cibc/visa/x1234=USD
```

Currency overrides are relative to the selected root. An institution root uses
`visa/x1234=USD`; the shared root uses `cibc/visa/x1234=USD`. Registered account
currencies are reused on later imports. Add `--apply --actor 'Paul La Rocque'`
after reviewing the preview.

The same shared root supports CIBC Canadian USD accounts under `cibc/us/x1234/`
and US-bank exports under `cibcus/smart/x5678/`. These products default to USD;
registered account currency remains authoritative. US-bank CSVs have headers
`Transaction Date`, `Short Description`, `Long Description`, `Amount`, and native
signed account changes. Positive native changes become ledger inflows (negative),
and negative changes become ledger outflows (positive). The long description is
used when present, otherwise the short description.

Long numeric references of eight or more digits in CIBC and CIBC US descriptions
are masked to their last four digits in database fields and row snapshots. The
original private exports stay unchanged; original text contributes only to the
one-way deduplication fingerprint. Source checksums, row numbers and archive paths
retain provenance. Account endings appear alongside transaction nicknames so
similar or duplicate labels remain distinguishable.

ACU supports `chequing` and `savings` folders. Its headered export has `Unique ID`,
`Date`, `Description`, `Amount` and `Balance` columns. Native positive changes
become ledger inflows (negative); native negative changes become outflows
(positive). Running balances remain in source-row evidence and do not establish
an opening/current account balance in the ledger. The repeated `Unique ID` field
is redacted in database row snapshots and is not used as a transaction ID. The
original private CSV remains unchanged, with its hash and row numbers retained.

Account nicknames are editable labels. Changing a nickname preserves the stable
reference, deduplication fingerprints, ownership and transaction links. They
appear in Accounts and the transaction list. Register or update profiles with a
private JSON list:

```json
[
  {"reference":"ACU Chequing x1234","institution":"ACU",
   "account_type":"chequing","last4":"1234","nickname":"Daily spending",
   "currency":"CAD"}
]
```

```sh
ledger --env-file .env.dev transactions account-profile --plan /private/accounts.json
ledger --env-file .env.dev transactions account-profile --plan /private/accounts.json \
  --apply --actor 'Paul La Rocque'
```

Profile registration creates metadata only, without importing transactions or
assigning owners. Actor/reason and before/after profile snapshots are audited.

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
