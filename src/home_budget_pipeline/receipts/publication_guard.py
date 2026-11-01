"""Durable normal-intake reservations; AMQP delivery remains at least once."""
from contextlib import contextmanager
import json
import uuid

LOCK_NAME = 'ledger:normal-intake'


@contextmanager
def serialized(conn):
    # Session lock survives the commits made by discovery and reservations.
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_lock(hashtextextended(%s, 0))', (LOCK_NAME,))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 0))', (LOCK_NAME,))
        conn.commit()


def eligible(conn, messages, queue):
    if not messages:
        return [], 0
    # Historical confirmed publications also count, including before this guard
    # was installed. Worker/result events cannot release an intake reservation:
    # persisted completion/cache state is checked separately by discovery.
    with conn.cursor() as cur:
        cur.execute('''SELECT DISTINCT ON (e.payload->>'message_id')
                   e.payload->>'message_id', e.payload->>'status'
            FROM lineage.receipt_events e
            WHERE e.payload->>'message_id' = ANY(%s)
              AND e.payload->>'queue' = ANY(%s)
              AND e.payload->>'status' IN ('intake_reserved','queued')
              AND NOT EXISTS (
                  SELECT 1 FROM lineage.receipt_events reset
                  WHERE reset.source_sha256=e.source_sha256
                    AND reset.payload->>'status'='intake_released'
                    AND reset.occurred_at > e.occurred_at)
            ORDER BY e.payload->>'message_id', e.occurred_at DESC, e.id DESC''',
            ([m.message_id for m in messages], [queue, queue.removesuffix('.work')+'.retry',
                                                queue.removesuffix('.work')+'.dead']))
        held = {row[0] for row in cur.fetchall()}
    selected = [m for m in messages if m.message_id not in held]
    return selected, len(messages) - len(selected)


def reserve(conn, message, queue, batch_id):
    # Commit BEFORE the broker call. A lost confirmation or process crash holds
    # the reservation for explicit recovery instead of silently duplicating work.
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO lineage.receipt_events
            (id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s::jsonb)''',
            (uuid.uuid4(), message.source_sha256, message.source_reference,
             json.dumps({'status':'intake_reserved','message_id':message.message_id,
                         'operation':message.message_type,'queue':queue,'batch_id':batch_id})))
    conn.commit()


def release(cur, sha, reference):
    """An explicit receipt retry releases old publication evidence atomically."""
    cur.execute('''INSERT INTO lineage.receipt_events
        (id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s::jsonb)''',
        (uuid.uuid4(), sha, reference,
         json.dumps({'status':'intake_released','reason':'explicit_receipt_retry'})))
