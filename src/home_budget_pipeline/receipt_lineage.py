"""Persist queue attempt observations independently of the derived graph."""
import logging
import os
import socket
import uuid

LOG = logging.getLogger(__name__)


def record(message, status, *, attempt_id=None, error_type=None, queue=None, result_runs=()):
    if message is None or not os.getenv('DATABASE_URL'):
        return
    payload = {'message_id': message.message_id, 'request_id': message.request_id,
               'batch_id': message.batch_id, 'operation': message.message_type,
               'attempt': message.attempt, 'attempt_id': attempt_id,
               'status': status, 'worker_host': socket.gethostname(),
               'worker_pid': os.getpid(), 'worker_node': os.getenv('K8S_NODE_NAME'),
               'error_type': error_type, 'queue': queue, 'result_runs': list(result_runs)}
    try:
        import psycopg
        from psycopg.types.json import Jsonb
        with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=2) as conn:
            conn.execute("SET LOCAL statement_timeout = '2s'")
            conn.execute('INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)',
                         (uuid.uuid4(), message.source_sha256, message.source_reference, Jsonb(payload)))
    except Exception as exc:
        # Evidence failure is visible, but cannot turn confirmed work into failure.
        LOG.error('lineage_evidence_unavailable message_id=%s status=%s error_type=%s',
                  message.message_id, status, type(exc).__name__)
