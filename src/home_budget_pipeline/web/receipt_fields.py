"""Shared receipt display rules: explicit extraction unknowns stop legacy fallbacks."""

RECEIPT_DATETIME_SQL = """(CASE
    WHEN re.transaction_datetime IS NOT NULL THEN re.transaction_datetime
    WHEN (re.raw_payload->'field_decisions') ? 'transaction_datetime' THEN NULL
    ELSE COALESCE(e.transaction_datetime, e.order_date::timestamp)
END)"""


def receipt_datetime(evidence, expense=None, filename_date=None):
    evidence, expense = evidence or {}, expense or {}
    value = evidence.get('transaction_datetime')
    decisions = (evidence.get('raw_payload') or {}).get('field_decisions') or {}
    if value is not None or 'transaction_datetime' in decisions:
        return value
    return expense.get('transaction_datetime') or expense.get('order_date') or filename_date


def receipt_date_note(evidence):
    decision = ((evidence.get('raw_payload') or {}).get('field_decisions') or {}).get('transaction_datetime') or {}
    if decision.get('status') == 'conflict':
        return 'Conflicting OCR date/time readings require review.'
    return ''


def duplicate_expense(receipt):
    value = receipt.get('duplicate_of')
    if value is None:
        value = ((receipt.get('raw_payload') or {}).get('duplicate_merge') or {}).get('canonical_expense_pk')
    return value if type(value) is int and value > 0 else None


def receipt_status(receipt):
    if receipt.get('deleted'):
        return 'deleted'
    if duplicate_expense(receipt):
        return 'duplicate'
    status = receipt.get('processing_status')
    if status:
        return status
    extraction = receipt.get('extraction_status')
    return 'review_required' if extraction in {'review', 'unreadable'} else extraction
