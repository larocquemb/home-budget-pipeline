from datetime import datetime, timedelta, timezone

from home_budget_pipeline.receipt_progress import summarize, queue_snapshot, reconcile_queue_snapshot


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
