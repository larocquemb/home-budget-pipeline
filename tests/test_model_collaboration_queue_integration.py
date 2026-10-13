"""Real RabbitMQ fan-out and durable PostgreSQL completion; no model API calls."""
import os
from threading import Barrier, Event, Thread
from uuid import uuid4, uuid5, UUID
import pytest
from home_budget_pipeline import model_collaboration_queue as queue
from home_budget_pipeline.receipt_model_providers import Profile

pytestmark = pytest.mark.integration


@pytest.fixture
def services(monkeypatch):
    dsn, url = os.getenv('TEST_DATABASE_URL'), os.getenv('TEST_RABBITMQ_URL')
    if not dsn or not url:
        pytest.skip('Requires test PostgreSQL and RabbitMQ')
    monkeypatch.setattr(queue, 'PREFIX', 'test.models.' + uuid4().hex)
    monkeypatch.setenv('COLLAB_TASK_TIMEOUT_SECONDS', '30')
    return dsn, url


def test_provider_tasks_overlap_and_duplicate_delivery_reuses_committed_result(services, monkeypatch):
    dsn, url = services
    stop, barrier = Event(), Barrier(2, timeout=10)
    calls, failures = [], []
    def handler(task, dsn):
        calls.append((task['stage'], task['profile']['provider']))
        barrier.wait()  # A serial implementation cannot complete either round.
        return {'status': 'success', 'provider': task['profile']['provider']}
    monkeypatch.setattr(queue, 'execute_task', handler)
    def worker(provider):
        try: queue.consume_loop(provider, dsn, url, stop)
        except Exception as exc: failures.append(exc)
    threads = [Thread(target=worker, args=(p,), daemon=True) for p in ('qwen', 'openai')]
    for thread in threads: thread.start()
    dispatcher = queue.Dispatcher(dsn, url)
    context = {'run_uuid': str(uuid4()), 'item_id': 1}
    profiles = [Profile(p, p, 'test') for p in ('qwen', 'openai')]
    try:
        for stage in ('independent', 'exchange'):
            results = dispatcher.round(stage, profiles, {}, context)
            assert all(r.get('status') == 'success' for r in results), (results, failures)
            assert {r['provider'] for r in results} == {'qwen', 'openai'}
            assert all(r['task_provenance']['worker_identity']['worker_host'] for r in results)
            if stage == 'independent': initial_results = results
        assert dispatcher.round('independent', profiles, {}, context) == initial_results
        stop.wait(.5)
        assert len(calls) == 4
        assert not failures
    finally:
        stop.set()
        for thread in threads: thread.join(10)
        for stage in ('independent', 'exchange'):
            cohort = uuid5(UUID(context['run_uuid']), f'1:{stage}')
            dispatcher.channel.queue_delete(queue=queue.PREFIX + '.results.' + str(cohort))
            dispatcher.store.conn.execute('DELETE FROM enrichment.model_collaboration_tasks WHERE cohort=%s', (cohort,))
        for provider in queue.PROVIDERS:
            dispatcher.channel.queue_delete(queue=queue.queue_name(provider))
        dispatcher.close()


def test_expired_tasks_do_not_call_models_and_completed_tasks_replay_safely(services):
    from datetime import datetime, timezone, timedelta
    dsn, _ = services
    store, cohort, task = queue.Store(dsn), uuid4(), uuid4()
    payload = {'reply': 'test.reply'}
    try:
        store.add(task, cohort, 'qwen', payload, datetime.now(timezone.utc) - timedelta(seconds=1))
        handler = lambda _: pytest.fail('Expired task must not execute')
        assert store.execute(task, 'qwen', handler) == 'test.reply'
        assert store.execute(task, 'qwen', handler) == 'test.reply'
        assert store.results(cohort)[str(task)]['error_type'] == 'TimeoutError'
        with pytest.raises(ValueError):
            store.add(task, cohort, 'qwen', {'reply': 'changed'}, datetime.now(timezone.utc))
    finally:
        store.conn.execute('DELETE FROM enrichment.model_collaboration_tasks WHERE cohort=%s', (cohort,))
        store.close()
