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
