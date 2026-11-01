"""Independent PostgreSQL sessions prove publisher serialization/recovery."""
import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from home_budget_pipeline.receipts import publication_guard as guard
from home_budget_pipeline.receipts.message import ReceiptMessage

pytestmark = pytest.mark.integration


@pytest.fixture
def job():
    return ReceiptMessage(hashlib.sha256(uuid.uuid4().bytes).hexdigest(), 'synthetic.pdf')


def connect():
    import psycopg
    return psycopg.connect(os.environ['TEST_DATABASE_URL'])


def event(conn, job, status, queue):
    conn.execute('INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s::jsonb)',
                 (uuid.uuid4(), job.source_sha256, job.source_reference,
                  json.dumps({'message_id':job.message_id,'status':status,'queue':queue})))
    conn.commit()


def test_two_concurrent_publishers_reserve_one_job(job):
    def publish(_):
        with connect() as conn, guard.serialized(conn):
            chosen, skipped = guard.eligible(conn, [job], 'guard.test.work')
            if chosen:
                guard.reserve(conn, job, 'guard.test.work', str(uuid.uuid4()))
            return len(chosen), skipped
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sorted(results) == [(0,1),(1,0)]


@pytest.mark.parametrize('state', ['queued','intake_reserved'])
def test_legacy_queue_evidence_and_new_reservations_survive_restart(job, state):
    with connect() as conn:
        event(conn, job, state, 'guard.restart.work')
    with connect() as conn, guard.serialized(conn):
        assert guard.eligible(conn, [job], 'guard.restart.work') == ([],1)
        assert guard.eligible(conn, [job], 'guard.other.work') == ([job],0)
        # An explicit retry records its release transactionally.
        with conn.cursor() as cur:
            guard.release(cur, job.source_sha256, job.source_reference)
        conn.commit()
        assert guard.eligible(conn, [job], 'guard.restart.work') == ([job],0)
        guard.reserve(conn, job, 'guard.restart.work', str(uuid.uuid4()))
        assert guard.eligible(conn, [job], 'guard.restart.work') == ([],1)


def test_crash_after_reservation_does_not_silently_republish(job):
    with connect() as conn:
        with pytest.raises(RuntimeError):
            with guard.serialized(conn):
                guard.reserve(conn, job, 'guard.crash.work', str(uuid.uuid4()))
                raise RuntimeError('Process failed before broker outcome known')
    with connect() as conn, guard.serialized(conn):
        assert guard.eligible(conn, [job], 'guard.crash.work') == ([],1)


def test_rollback_of_retry_preserves_publication_reservation(job):
    with connect() as conn:
        guard.reserve(conn, job, 'guard.rollback.work', str(uuid.uuid4()))
        with conn.cursor() as cur:
            guard.release(cur, job.source_sha256, job.source_reference)
        conn.rollback()
        assert guard.eligible(conn, [job], 'guard.rollback.work') == ([],1)


def test_partial_broker_failure_only_reserves_attempted_jobs(job, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from home_budget_pipeline.receipts import queue_ingest as queue
    jobs = [job] + [ReceiptMessage(hashlib.sha256(uuid.uuid4().bytes).hexdigest(), f'{i}.pdf') for i in (2,3)]
    monkeypatch.setattr(queue, 'publication_plan', lambda *a, **kw: (jobs, {'published':3}))
    monkeypatch.setattr(queue, 'connect_broker', lambda _: MagicMock())
    monkeypatch.setattr(queue.receipt_lineage, 'start_batch', lambda *a: None)
    count = 0
    def publish(*a, **kw):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('Lost broker confirmation')
    monkeypatch.setattr(queue, 'publish_confirmed', publish)
    args = SimpleNamespace(db_dsn=os.environ['TEST_DATABASE_URL'], rabbitmq_url='mock',
                           receipt_root=tmp_path, ingest_schema='ingest', ocr_cache=tmp_path)
    with pytest.raises(RuntimeError, match='reservation retained'):
        queue._publish_normal(args, queue.Topology('guard.partial'))
    with connect() as conn, guard.serialized(conn):
        assert guard.eligible(conn, jobs, 'guard.partial.work') == ([jobs[2]],2)
