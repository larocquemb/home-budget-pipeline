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
    current = request_processing_status(receipt)
    if current:
        return current
    evidence = receipt.get('evidence') or receipt
    payload = evidence.get('raw_payload') or {}
    scan = (payload.get('field_decisions') or {}).get('scan_completeness') or {}
    if (receipt.get('incomplete_scan')
            or 'possible_missing_page_content' in (payload.get('review_reasons') or [])
            or scan.get('value') == 'possible_page_gap'):
        return 'incomplete_scan'
    if receipt.get('request_completed_at'):
        extraction=receipt.get('extraction_status') or evidence.get('extraction_status')
        if extraction in {'complete','review','unreadable'}:
            return 'succeeded' if extraction=='complete' else 'review_required'
    status = receipt.get('processing_status')
    if status:
        return status
    extraction = receipt.get('extraction_status')
    return 'review_required' if extraction in {'review', 'unreadable'} else extraction


# Choose the newest submitted request, not the newest heartbeat from an older
# request. Completion must belong to that exact request, never the old receipt.
LATEST_REQUEST_JOIN = """
LEFT JOIN LATERAL (
    SELECT payload->>'request_id' AS request_id, MIN(occurred_at) AS submitted_at
      FROM lineage.receipt_events
     WHERE source_sha256=r.source_sha256 AND payload->>'request_id' IS NOT NULL
       AND payload->>'operation' IN ('receipt.reprocess.v2','receipt.cache-rebuild.v3')
     GROUP BY payload->>'request_id' ORDER BY MIN(occurred_at) DESC LIMIT 1
) lr ON TRUE
LEFT JOIN LATERAL (
    SELECT payload->>'status' AS status, occurred_at
      FROM lineage.receipt_events
     WHERE source_sha256=r.source_sha256 AND payload->>'request_id'=lr.request_id
       AND payload->>'status' NOT IN ('batch_started','intake_reserved')
     ORDER BY COALESCE((payload->>'attempt')::int,1) DESC,
              (payload->>'status'<>'queued') DESC, occurred_at DESC, id DESC LIMIT 1
) le ON TRUE
LEFT JOIN LATERAL (
    SELECT MAX(completed_at) AS completed_at FROM (
        SELECT completed_at FROM budget.receipt_reprocess_requests
         WHERE request_id::text=lr.request_id AND completed_at>=lr.submitted_at
        UNION ALL
        SELECT completed_at FROM budget.receipt_cache_requests
         WHERE request_id::text=lr.request_id AND completed_at>=lr.submitted_at
    ) committed
) lc ON TRUE
"""
LATEST_REQUEST_COLUMNS = """lr.request_id AS latest_request_id,
    lr.submitted_at AS request_submitted_at, le.status AS request_status,
    le.occurred_at AS request_observed_at, lc.completed_at AS request_completed_at"""


def request_processing_status(receipt, now=None):
    from datetime import datetime,timezone,timedelta
    if not receipt.get('latest_request_id') or receipt.get('request_completed_at'):
        return None
    status=receipt.get('request_status')
    if status in {'queued','retried'}:
        return 'queued'
    if status in {'active','heartbeat'}:
        observed=receipt.get('request_observed_at')
        if isinstance(observed,str):
            observed=datetime.fromisoformat(observed)
        if observed and (now or datetime.now(timezone.utc))-observed<=timedelta(seconds=90):
            return 'processing'
        return 'progress_unknown'
    if status in {'cache_results_published','cache_results_republished','results_published','review_results_published'}:
        return 'saving_results'
    return 'failed' if status=='failed' else 'progress_unknown'
