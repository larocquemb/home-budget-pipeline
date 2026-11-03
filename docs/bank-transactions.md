# Bank transactions and receipt cross-reference

Ledger keeps bank transactions separate from receipt extraction. A bank date or
merchant description is corroborating evidence; confirming an allocation does
not rewrite OCR, receipt fields, or receipt line items.

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
