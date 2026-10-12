"""Verify actual PostgreSQL start/end pairing, including overlapping attempts."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4
import pytest


@pytest.mark.integration
def test_processing_report_keeps_overlapping_attempts_separate():
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    source = f'report-test-{uuid4()}.pdf'
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    first, second, stale = str(uuid4()), str(uuid4()), str(uuid4())
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], row_factory=dict_row) as conn:
        try:
            for attempt, pid, status, offset in [
                (first,100,'active',0), (second,200,'active',60),
                (first,100,'results_published',120), (second,200,'heartbeat',290),
                (stale,300,'active',0)]:
                conn.execute('''INSERT INTO lineage.receipt_events
                    (id,source_sha256,source_reference,occurred_at,payload)
                    VALUES (%s,%s,%s,%s,%s)''',
                    (uuid4(), 'a'*64, source, started+timedelta(seconds=offset),
                     Jsonb(dict(attempt_id=attempt, worker_host='report-worker',
                                worker_pid=pid,status=status, **({'cache_hit':False} if status=='results_published' else {})))))
            rows = conn.execute(Path('sql/reports/receipt_processing_timeline.sql').read_text()).fetchall()
            rows = [row for row in rows if row['receipt'] == source]
            assert len(rows) == 3
            by_worker = {row['worker_pid']: row for row in rows}
            completed, live, abandoned = by_worker['100'], by_worker['200'], by_worker['300']
            assert completed['elapsed_minutes'] == 2
            assert completed['finished_at'] == started + timedelta(minutes=2)
            assert completed['live_state'] == 'results_published'
            assert completed['cache_hit'] is False
            assert live['finished_at'] is None
            assert live['status'] == 'active'
            assert live['live_state'] == 'processing'
            assert live['started_at'] < completed['finished_at']
            assert abandoned['live_state'] == 'heartbeat_stale'
        finally:
            conn.rollback()
