"""Read-only development progress snapshots; no receipt contents leave the host."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SUCCESS = {'cache_results_published', 'cache_results_republished',
           'results_published', 'review_results_published'}


def summarize(rows, now=None):
    """Count each request once; an expired heartbeat is unknown, never complete."""
    now = now or datetime.now(timezone.utc)
    counts = dict(total=len(rows), waiting=0, running=0, completed=0, failed=0, unknown=0)
    for row in rows:
        status = row['payload']['status']
        if status in SUCCESS:
            key = 'completed'
        elif status == 'failed':
            key = 'failed'
        elif status in {'active', 'heartbeat'}:
            key = 'running' if (now - row['occurred_at']).total_seconds() <= 90 else 'unknown'
        elif status in {'queued', 'retried'}:
            key = 'waiting'
        else:
            key = 'unknown'
        counts[key] += 1
    return counts


def database_snapshot(conn):
    """Use acknowledged publication/worker observations, not time-window counters."""
    batches = conn.execute("""
        SELECT payload->>'batch_id' AS batch_id, MIN(occurred_at) AS started_at
          FROM lineage.receipt_events
         WHERE payload->>'batch_id' IS NOT NULL
         GROUP BY 1 ORDER BY started_at DESC LIMIT 1
    """).fetchone()
    result = {'event': 'receipt_progress_snapshot', 'observed_at': datetime.now(timezone.utc).isoformat()}
    result['database_receipts'] = conn.execute('SELECT COUNT(DISTINCT source_sha256) FROM budget.receipt_evidence').fetchone()['count']
    result['database_expenses'] = conn.execute('SELECT COUNT(*) FROM budget.expenses').fetchone()['count']
    result['database_failed'] = conn.execute(
        "SELECT COUNT(*) FROM ingest.receipt_processing_status WHERE status = 'failed'"
    ).fetchone()['count']
    if not batches:
        result.update(batch_id='none', batch_available=0)
        return result
    batch_id = batches['batch_id']
    rows = conn.execute("""
        SELECT DISTINCT ON (payload->>'request_id') payload, occurred_at, source_sha256
          FROM lineage.receipt_events
         WHERE payload->>'batch_id' = %s AND payload->>'request_id' IS NOT NULL
         ORDER BY payload->>'request_id', (payload->>'attempt')::int DESC,
                  (payload->>'status' <> 'queued') DESC, occurred_at DESC, id DESC
    """, (batch_id,)).fetchall()
    result.update(summarize(rows), batch_id=batch_id, batch_available=1,
                  batch_started_at=batches['started_at'].isoformat(),
                  batch_started_local=batches['started_at'].astimezone(ZoneInfo('America/Winnipeg')).strftime('%Y-%m-%d %H:%M %Z'),
                  batch_last_observed_at=max(row['occurred_at'] for row in rows).isoformat())
    # Presence in evidence is separate from cache completion or result publication.
    hashes = list({row['source_sha256'] for row in rows})
    result['batch_imported'] = conn.execute(
        'SELECT COUNT(DISTINCT source_sha256) FROM budget.receipt_evidence WHERE source_sha256 = ANY(%s)',
        (hashes,),
    ).fetchone()['count']
    return result


def queue_snapshot(connection):
    """Passive declarations inspect existing queues without consuming any messages."""
    result = {}
    for name, queue in [('queue_waiting', 'receipts.v1.work'),
                        ('queue_retry', 'receipts.v1.retry'),
                        ('queue_failed', 'receipts.v1.dead'),
                        ('collector_waiting', 'ocr.results.v1.work'),
                        ('collector_failed', 'ocr.results.v1.dead')]:
        channel = connection.channel()
        try:
            result[name] = channel.queue_declare(queue=queue, passive=True).method.message_count
        finally:
            if channel.is_open:
                channel.close()
    return result


def reconcile_queue_snapshot(result, now=None):
    """An old publication event is not proof a receipt remains on an empty broker."""
    now = now or datetime.now(timezone.utc)
    started = result.get('batch_started_at')
    old = started and (now - datetime.fromisoformat(started)).total_seconds() > 90
    if (old and result.get('queue_available') and result.get('queue_waiting') == 0
            and result.get('queue_retry') == 0 and result.get('waiting', 0)
            and result.get('running', 0) == 0):
        result['unknown'] += result['waiting']
        result['waiting'] = 0
    observed = result.get('batch_last_observed_at', started)
    if not observed:
        result['batch_state'] = 'no recorded batch'
    elif not result.get('queue_available'):
        result['batch_state'] = 'queue unavailable'
    elif result.get('running', 0) or result.get('queue_waiting', 0) or result.get('queue_retry', 0):
        result['batch_state'] = 'active'
    elif (now - datetime.fromisoformat(observed)).total_seconds() > 86400:
        result['batch_state'] = 'historical'
        # Missing historical outcomes are not evidence of zero completions.
        if result.get('unknown', 0):
            for field in ('completed', 'failed'):
                if result.get(field) == 0:
                    result.pop(field)
    else:
        result['batch_state'] = 'idle'
    result['completed_known'] = int('completed' in result)
    return result
