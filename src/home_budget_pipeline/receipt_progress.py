"""Read-only development progress snapshots; no receipt contents leave the host."""
from datetime import datetime, timezone, timedelta
import json
import re
from zoneinfo import ZoneInfo

SUCCESS = {'cache_results_published', 'cache_results_republished',
           'results_published', 'review_results_published', 'persisted'}


def worker_observations(streams, requests):
    """Correlate heartbeats with the same worker's explicitly identified request."""
    allowed = {r['payload']['request_id'] for r in requests}
    records = []
    for stream in streams:
        for _, line in stream.get('values', []):
            try:
                data = json.loads(line)
                when = datetime.fromisoformat(data['timestamp'])
                if when.utcoffset() is not None:
                    records.append((when, data))
            except (ValueError, KeyError, TypeError):
                continue
    observations, active = [], {}
    for when, data in sorted(records, key=lambda r: r[0]):
        message = data.get('message', '')
        request = re.search(r'\brequest_id=([0-9a-f-]{36})\b', message)
        if not request:
            request = re.search(r'\breceipt=(receipt\.v1:[0-9a-f]{64})\b', message)
        status = re.search(r'\bstatus=(\w+)\b', message)
        host = re.search(r'\bworker_host=(\S+)', message)
        reference = re.search(r'\bsource_reference=(.*?) request_id=', message)
        if status and host and reference:
            key = (host[1], reference[1])
            if status[1] == 'active' and request and request[1] in allowed:
                active[key] = request[1]
            else:
                active.pop(key, None)
        if request and status and request[1] in allowed:
            observations.append((when, request[1], status[1]))
        elif data.get('event') == 'receipt_processing_state' and data.get('processing_state') == 1:
            request_id = active.get((data.get('worker_host'), data.get('receipt_name')))
            if request_id:
                observations.append((when, request_id, 'heartbeat'))
    return observations


def apply_worker_observations(result, streams=(), *, available=True, complete=True, now=None):
    """Worker publication proves cache progress; committed requests prove persistence."""
    now = now or datetime.now(timezone.utc)
    rows = {r['payload']['request_id']: dict(r, payload=dict(r['payload']))
            for r in result.pop('_requests', [])}
    seen = set()
    for when, request, status in worker_observations(streams, list(rows.values())):
        row = rows.get(request)
        if row is not None and when >= row['occurred_at']:
            row['payload']['status'] = status
            row['occurred_at'] = when
            seen.add(request)
    if not available or not complete:
        for request, row in rows.items():
            if request not in seen and row['payload']['status'] == 'queued' and now-row['occurred_at'] > timedelta(seconds=90):
                row['payload']['status'] = 'unknown'
    persisted = result.pop('_persisted', {})
    for request, when in persisted.items():
        if request in rows:
            rows[request]['payload']['status'] = 'persisted'
            rows[request]['occurred_at'] = when
    if rows:
        result.update(summarize(rows.values(), now))
        result['batch_last_observed_at'] = max(r['occurred_at'] for r in rows.values()).isoformat()
    result['unknown'] = result.get('unknown', 0) + result.get('publication_unconfirmed', 0)
    result['total'] = len(rows) + result.get('publication_unconfirmed', 0)
    result.update(worker_observations_available=int(available),
                  worker_observations_complete=int(complete), batch_persisted=len(persisted))
    return result


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
        SELECT payload->>'batch_id' AS batch_id, MIN(occurred_at) AS started_at,
               MAX((payload->>'batch_total')::int) AS planned
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
        result.update(batch_id='none', batch_available=0, batch_label='No recorded batch',
                      batch_started_local='unknown')
        return result
    batch_id = batches['batch_id']
    rows = conn.execute("""
        SELECT DISTINCT ON (COALESCE(payload->>'request_id', payload->>'message_id'))
               payload, occurred_at, source_sha256, source_reference,
               COALESCE(payload->>'request_id', payload->>'message_id') AS request_key
          FROM lineage.receipt_events
         WHERE payload->>'batch_id' = %s
           AND COALESCE(payload->>'request_id', payload->>'message_id') IS NOT NULL
         ORDER BY COALESCE(payload->>'request_id', payload->>'message_id'), (payload->>'attempt')::int DESC,
                  (payload->>'status' <> 'queued') DESC, occurred_at DESC, id DESC
    """, (batch_id,)).fetchall()
    rows = [dict(row, payload=dict(row['payload'], request_id=row['request_key'])) for row in rows]
    result.update(summarize(rows), batch_id=batch_id, batch_available=1,
                  batch_started_at=batches['started_at'].isoformat(),
                  batch_started_local=batches['started_at'].astimezone(ZoneInfo('America/Winnipeg')).strftime('%Y-%m-%d %H:%M %Z'),
                  batch_last_observed_at=max((row['occurred_at'] for row in rows), default=batches['started_at']).isoformat())
    result['publication_unconfirmed'] = max(0, (batches['planned'] or len(rows)) - len(rows))
    result['batch_label'] = 'Batch ' + batch_id
    operations = {row['payload'].get('operation') for row in rows}
    result['batch_kind'] = next(iter(operations)) if len(operations) == 1 else 'receipt-publication'
    result['_requests'] = rows
    request_ids = [row['payload']['request_id'] for row in rows]
    persisted = conn.execute("""
        SELECT request_id::text, completed_at FROM budget.receipt_reprocess_requests
         WHERE request_id::text = ANY(%s) AND completed_at >= %s
        UNION ALL
        SELECT request_id::text, completed_at FROM budget.receipt_cache_requests
         WHERE request_id::text = ANY(%s) AND completed_at >= %s
    """, (request_ids, batches['started_at'], request_ids, batches['started_at'])).fetchall()
    result['_persisted'] = {row['request_id']: row['completed_at'] for row in persisted}
    normal = {row['source_sha256']: row['payload']['request_id'] for row in rows
              if row['payload'].get('operation') == 'receipt.process.v1'}
    if normal:
        completed = conn.execute("""
            SELECT source_sha256, completed_at FROM ingest.receipt_processing_status
             WHERE source_sha256 = ANY(%s) AND completed_at >= %s
               AND status IN ('succeeded','review_required')
        """, (list(normal), batches['started_at'])).fetchall()
        result['_persisted'].update({normal[row['source_sha256']]: row['completed_at'] for row in completed})
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
    elif (now - datetime.fromisoformat(observed)).total_seconds() > 86400:
        result['batch_state'] = 'historical'
        result['unknown'] = result.get('unknown', 0) + result.get('waiting', 0)
        result['waiting'] = 0
        # Missing historical outcomes are not evidence of zero completions.
        if result.get('unknown', 0):
            for field in ('completed', 'failed'):
                if result.get(field) == 0:
                    result.pop(field)
    elif result.get('publication_unconfirmed', 0):
        result['batch_state'] = 'publication incomplete'
    elif result.get('running', 0) or result.get('waiting', 0):
        result['batch_state'] = 'active'
    elif result.get('unknown', 0):
        result['batch_state'] = 'incomplete observations'
    elif result.get('completed', 0) > result.get('batch_persisted', result.get('completed', 0)):
        result['batch_state'] = 'saving results'
    elif result.get('total', 0) and result.get('completed', 0) + result.get('failed', 0) == result['total']:
        result['batch_state'] = 'finished with failures' if result.get('failed') else 'complete'
    else:
        result['batch_state'] = 'idle'
    result['completed_known'] = int('completed' in result)
    return result
