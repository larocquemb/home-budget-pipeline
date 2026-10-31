from datetime import datetime, timedelta, timezone

from home_budget_pipeline.receipt_progress import summarize, queue_snapshot, reconcile_queue_snapshot
from home_budget_pipeline.receipt_progress import apply_worker_observations, database_snapshot
import json


def test_batch_states_partition_requests_and_expired_workers_are_unknown():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    statuses = ['queued', 'retried', 'active', 'heartbeat', 'failed',
                'cache_results_published', 'cache_results_republished', 'unexpected']
    rows = [{'payload': {'status': status}, 'occurred_at': now} for status in statuses]
    rows[3]['occurred_at'] = now - timedelta(seconds=91)
    counts = summarize(rows, now)
    assert counts == {'total': 8, 'waiting': 2, 'running': 1, 'completed': 2, 'failed': 1, 'unknown': 2}
    assert sum(value for key, value in counts.items() if key != 'total') == counts['total']


def test_empty_batch_is_zero_not_a_synthetic_completed_receipt():
    assert summarize([])['total'] == 0
    assert summarize([])['completed'] == 0


def test_historical_publications_are_unknown_when_the_actual_queue_is_empty():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    state = {'batch_started_at': (now - timedelta(days=2)).isoformat(),
             'queue_available': 1, 'queue_waiting': 0, 'queue_retry': 0,
             'waiting': 128, 'running': 0, 'unknown': 0}
    assert reconcile_queue_snapshot(dict(state), now)['unknown'] == 128
    assert reconcile_queue_snapshot(dict(state), now)['waiting'] == 0
    state['queue_available'] = 0
    assert reconcile_queue_snapshot(dict(state), now)['waiting'] == 128


def test_missing_historical_outcomes_are_not_reported_as_zero_completions():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    result = reconcile_queue_snapshot({
        'batch_started_at': (now - timedelta(days=20)).isoformat(),
        'queue_available': 1, 'queue_waiting': 0, 'queue_retry': 0,
        'waiting': 128, 'running': 0, 'unknown': 0, 'completed': 0, 'failed': 0,
    }, now)
    assert result['batch_state'] == 'historical'
    assert result['unknown'] == 128
    assert 'completed' not in result and 'failed' not in result
    assert result['completed_known'] == 0


def test_live_batch_zero_is_known_and_remains_numeric():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    result = reconcile_queue_snapshot({
        'batch_started_at': now.isoformat(), 'queue_available': 1,
        'queue_waiting': 148, 'queue_retry': 0,
        'waiting': 148, 'running': 2, 'unknown': 0, 'completed': 0, 'failed': 0,
    }, now)
    assert result['batch_state'] == 'active'
    assert result['completed'] == 0 and result['failed'] == 0
    assert result['completed_known'] == 1


def test_queue_inspection_is_passive_and_never_fetches_or_acknowledges_messages():
    class Channel:
        is_open = True
        def queue_declare(self, *, queue, passive):
            assert passive is True
            calls.append(queue)
            return type('Reply', (), {'method': type('Method', (), {'message_count': 7})()})()
        def close(self):
            self.is_open = False
    class Broker:
        def channel(self):
            return Channel()
    calls = []
    counts = queue_snapshot(Broker())
    assert counts['queue_waiting'] == 7
    assert counts['collector_failed'] == 7
    assert len(calls) == 5


def test_unconfirmed_publications_are_unknown_not_queued_or_completed():
    result = apply_worker_observations({'_requests': [], 'publication_unconfirmed': 5})
    assert result['total'] == result['unknown'] == 5
    assert result.get('waiting', 0) == result.get('completed', 0) == 0


def test_unbatched_requests_never_create_a_dashboard_batch():
    from types import SimpleNamespace
    class Connection:
        def execute(self, sql):
            if 'lineage.receipt_events' in sql:
                assert "WHERE payload->>'batch_id' IS NOT NULL" in sql
                return SimpleNamespace(fetchone=lambda: None)
            return SimpleNamespace(fetchone=lambda: {'count': 151})
    result = database_snapshot(Connection())
    assert result['batch_available'] == 0 and result['batch_id'] == 'none'
    assert result['database_receipts'] == 151


def test_worker_logs_deduplicate_transport_and_keep_persistence_separate():
    now = datetime(2026, 10, 31, 10, 40, tzinfo=timezone.utc)
    ids = ['a'*8+'-aaaa-aaaa-aaaa-'+'a'*12, 'b'*8+'-bbbb-bbbb-bbbb-'+'b'*12]
    rows = [{'payload': {'request_id': request, 'status': 'queued'},
             'source_reference': str(i)+'.pdf', 'occurred_at': now-timedelta(minutes=10)}
            for i, request in enumerate(ids)]
    messages = [
        {'timestamp': (now-timedelta(seconds=20)).isoformat(), 'message': f'request_id={ids[0]} status=results_published'},
        {'timestamp': (now-timedelta(minutes=3)).isoformat(), 'message': f'worker_host=pod source_reference=1.pdf request_id={ids[1]} status=active'},
        {'timestamp': now.isoformat(), 'message': 'receipt_processing_state', 'event': 'receipt_processing_state',
         'receipt_name': '1.pdf', 'worker_host': 'pod', 'processing_state': 1},
    ]
    stream = {'values': [[str(i), json.dumps(message)] for i, message in enumerate(messages)]}
    result = apply_worker_observations({'_requests': rows, '_persisted': {}}, [stream, stream], now=now)
    assert result['total'] == 2 and result['completed'] == 1 and result['running'] == 1
    assert result['batch_persisted'] == 0
    assert not any(key.startswith('_') for key in result)


def test_heartbeat_for_unrelated_work_cannot_refresh_a_batch_request():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    request = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    messages = [
        {'timestamp': (now-timedelta(minutes=4)).isoformat(), 'message': f'worker_host=pod source_reference=a.pdf request_id={request} status=active'},
        {'timestamp': (now-timedelta(minutes=3)).isoformat(), 'message': 'worker_host=pod source_reference=a.pdf request_id=None status=active'},
        {'timestamp': now.isoformat(), 'message': 'receipt_processing_state', 'event': 'receipt_processing_state',
         'receipt_name': 'a.pdf', 'worker_host': 'pod', 'processing_state': 1},
    ]
    stream = {'values': [[str(i), json.dumps(m)] for i, m in enumerate(messages)]}
    result = apply_worker_observations({'_requests': [{'payload': {'request_id': request, 'status': 'queued'},
        'source_reference': 'a.pdf', 'occurred_at': now-timedelta(minutes=5)}]}, [stream], now=now)
    assert result['running'] == 0 and result['unknown'] == 1


def test_committed_request_remains_complete_when_logs_are_unavailable():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    rows = [{'payload': {'request_id': request, 'status': 'queued'},
             'source_reference': request+'.pdf', 'occurred_at': now-timedelta(minutes=10)}
            for request in ['one', 'two']]
    result = apply_worker_observations({'_requests': rows, '_persisted': {'one': now}}, available=False, now=now)
    assert result['completed'] == result['unknown'] == 1
    assert result['waiting'] == 0 and result['batch_persisted'] == 1


def test_unrelated_queue_activity_does_not_reactivate_old_batch():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    result = reconcile_queue_snapshot({'batch_started_at': (now-timedelta(days=20)).isoformat(),
        'queue_available': 1, 'queue_waiting': 99, 'queue_retry': 0,
        'waiting': 128, 'running': 0, 'unknown': 0, 'completed': 0, 'failed': 0}, now)
    assert result['batch_state'] == 'historical'
    assert result['waiting'] == 0 and result['unknown'] == 128
    assert result['completed_known'] == 0


def test_cache_completion_waits_for_collector_persistence():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    result = reconcile_queue_snapshot({'batch_started_at': now.isoformat(), 'queue_available': 1,
        'queue_waiting': 0, 'queue_retry': 0, 'running': 0, 'waiting': 0, 'unknown': 0,
        'total': 151, 'completed': 151, 'batch_persisted': 79, 'failed': 0}, now)
    assert result['batch_state'] == 'saving results'
    result['batch_persisted'] = 151
    assert reconcile_queue_snapshot(result, now)['batch_state'] == 'complete'
