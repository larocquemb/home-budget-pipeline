"""Durable model fan-out and evidence exchange through RabbitMQ.

Broker messages contain task IDs only. PostgreSQL stores immutable inputs and
results; workers commit before publishing a completion and acknowledging work.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import time
import socket
from uuid import UUID, uuid5

from . import receipt_collaboration as collaboration
from . import receipt_model_providers as providers
from . import receipt_shared_evidence as shared
from .brave_query_cache import BraveQueryCache
from .receipts.queue_ingest import connect_broker, publish_confirmed

PREFIX = 'receipt-models.v1'
PROVIDERS = ('qwen', 'openai', 'anthropic', 'gemini')
DDL = '''CREATE TABLE IF NOT EXISTS enrichment.model_collaboration_tasks (
    id UUID PRIMARY KEY, cohort UUID NOT NULL, provider TEXT NOT NULL,
    input JSONB NOT NULL, result JSONB, attempts INTEGER NOT NULL DEFAULT 0,
    deadline TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS model_collaboration_tasks_cohort_idx
ON enrichment.model_collaboration_tasks(cohort)'''


def queue_name(provider):
    if provider not in PROVIDERS:
        raise ValueError('Unknown collaboration worker provider')
    return PREFIX + '.' + provider


def declare(channel, provider=None, reply=None):
    for name in ([queue_name(provider)] if provider else [queue_name(p) for p in PROVIDERS]):
        channel.queue_declare(queue=name, durable=True, arguments={'x-queue-type': 'quorum'})
    if reply:
        channel.queue_declare(queue=reply, durable=True,
                              arguments={'x-queue-type': 'quorum', 'x-expires': 86400000})
    channel.confirm_delivery()


def notification(channel, queue, task_id):
    publish_confirmed(channel, '', json.dumps({'task_id': str(task_id)}).encode(),
                      routing_key=queue, message_id=str(task_id), message_type='receipt.model.v1')


class Store:
    def __init__(self, dsn):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10)
        self.conn.execute('SELECT pg_advisory_lock(78344002)')
        try:
            self.conn.execute(DDL)
        finally:
            self.conn.execute('SELECT pg_advisory_unlock(78344002)')

    def close(self):
        self.conn.close()

    def add(self, task_id, cohort, provider, payload, deadline):
        from psycopg.types.json import Jsonb
        self.conn.execute('''INSERT INTO enrichment.model_collaboration_tasks
            (id,cohort,provider,input,deadline) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO NOTHING''',
            (task_id, cohort, provider, Jsonb(json.loads(json.dumps(payload, default=str))), deadline))
        existing = self.conn.execute('SELECT input FROM enrichment.model_collaboration_tasks WHERE id=%s', (task_id,)).fetchone()
        if existing['input'] != json.loads(json.dumps(payload, default=str)):
            raise ValueError('Task identity reused with different inputs')

    def results(self, cohort):
        return {str(r['id']): r['result'] for r in self.conn.execute(
            'SELECT id,result FROM enrichment.model_collaboration_tasks WHERE cohort=%s AND result IS NOT NULL',
            (cohort,)).fetchall()}

    def execute(self, task_id, provider, handler):
        from psycopg.types.json import Jsonb
        lock = int.from_bytes(hashlib.sha256(str(task_id).encode()).digest()[:8], 'big', signed=True)
        if not self.conn.execute('SELECT pg_try_advisory_lock(%s) AS locked', (lock,)).fetchone()['locked']:
            return None  # A duplicate delivery; the active holder will notify.
        try:
            task = self.conn.execute('SELECT * FROM enrichment.model_collaboration_tasks WHERE id=%s', (task_id,)).fetchone()
            if task is None or task['provider'] != provider:
                raise ValueError('Unknown task or wrong provider routing')
            reply = task['input']['reply']
            if task['result'] is None:
                attempts = task['attempts']
                self.conn.execute('UPDATE enrichment.model_collaboration_tasks SET attempts=attempts+1 WHERE id=%s', (task_id,))
                started_at = datetime.now(timezone.utc).isoformat()
                try:
                    if datetime.now(timezone.utc) >= task['deadline']:
                        raise TimeoutError('Task deadline expired')
                    if attempts >= 2:
                        raise RuntimeError('Interrupted task retry budget exhausted')
                    result = handler(task['input'])
                    if datetime.now(timezone.utc) >= task['deadline']:
                        raise TimeoutError('Task completed after deadline')
                except Exception as exc:
                    result = {'status': 'error', 'error_type': type(exc).__name__}
                result['task_provenance'] = {'task_id': str(task_id), 'stage': task['input'].get('stage'),
                    'started_at': started_at, 'worker_identity': {'worker_host': socket.gethostname(),
                    'worker_pid': os.getpid(), 'worker_node': os.getenv('K8S_NODE_NAME')}}
                self.conn.execute('''UPDATE enrichment.model_collaboration_tasks
                    SET result=%s,completed_at=NOW() WHERE id=%s''', (Jsonb(result), task_id))
            return reply
        finally:
            self.conn.execute('SELECT pg_advisory_unlock(%s)', (lock,))


class Dispatcher:
    def __init__(self, dsn, url):
        self.store = Store(dsn)
        try:
            self.connection = connect_broker(url)
            self.channel = self.connection.channel()
            declare(self.channel)
        except Exception:
            self.store.close()
            raise

    def close(self):
        try:
            if self.connection.is_open:
                self.connection.close()
        finally:
            self.store.close()

    def round(self, stage, profiles, payload, context):
        timeout = int(os.getenv('COLLAB_TASK_TIMEOUT_SECONDS', '900'))
        if not 30 <= timeout <= 3600:
            raise ValueError('COLLAB_TASK_TIMEOUT_SECONDS must be 30..3600')
        cohort = uuid5(UUID(context['run_uuid']), f"{context['item_id']}:{stage}")
        reply = PREFIX + '.results.' + str(cohort)
        declare(self.channel, reply=reply)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        ids = [uuid5(cohort, p.name) for p in profiles]
        for task_id, profile in zip(ids, profiles):
            task = {**payload, 'profile': asdict(profile), 'stage': stage,
                    'context': context, 'reply': reply}
            self.store.add(task_id, cohort, profile.provider, task, deadline)
        # Persist the entire round before publishing any member; every enabled
        # provider is dispatched before the coordinator waits for a result.
        for task_id, profile in zip(ids, profiles):
            notification(self.channel, queue_name(profile.provider), task_id)
        from . import product_comparison as comparison
        comparison.emit('enrichment_collaboration_tasks_dispatched', {**context, 'stage': stage,
            'tasks': [{'task_id': str(task_id), 'profile': p.name, 'provider': p.provider}
                      for task_id, p in zip(ids, profiles)]})
        end = time.monotonic() + timeout
        while True:
            results = self.store.results(cohort)
            if all(str(task_id) in results for task_id in ids) or time.monotonic() >= end:
                break
            method, _, _ = self.channel.basic_get(reply, auto_ack=False)
            if method:
                self.channel.basic_ack(method.delivery_tag)
            else:
                self.connection.sleep(.2)
        comparison.emit('enrichment_collaboration_round_completed', {**context, 'stage': stage,
            'expected_tasks': len(ids), 'completed_tasks': len(results)})
        return [results.get(str(task_id), {'status': 'error', 'error_type': 'TimeoutError'}) for task_id in ids]


def peer_review(task):
    profile = providers.Profile(**task['profile'])
    bundle, candidates = task['bundle'], task['candidates']
    images, image_errors = shared.render_images(bundle) if profile.vision else ([], [])
    image_metadata = [{k: v for k, v in image.items() if k != 'data'} for image in images]
    budget = max(0, (profile.context_tokens - profile.output_tokens -
                    collaboration.IMAGE_TOKEN_RESERVE * len(images)) * 2 - 8000)
    packed, coverage = collaboration.pack_sources(bundle['sources'], budget)
    sources = {s['id'] for entry in packed for s in entry['observations']}
    selectable = [{**c, 'ocr_support': [s for s in c['ocr_support'] if s in sources]} for c in candidates]
    text = collaboration.INSTRUCTIONS + '''Evidence exchange: compare every independent finding,
including peer abstentions and conflicting OCR readings. You may revise your first
review using the pooled receipt observations and retailer evidence. Other models'
claims are hypotheses, not new receipt evidence or independent verification.
Select a retailer_results candidate and its exact title, cite its product ID and
supporting receipt observation IDs, or abstain with all product fields null.\n''' + json.dumps({
        'receipt_item': task['row'], 'ocr_evidence': packed,
        'receipt_checks': bundle['validations'], 'retailer_results': [collaboration.prompt_candidate(c) for c in selectable],
        'independent_findings': task['findings'], 'attached_images': image_metadata}, default=str)
    review = collaboration.review_with_citations(profile, text,
        sources | {c['id'] for c in selectable} | {i['id'] for i in images}, selectable, images, task['context'])
    review['exchange_coverage'] = coverage
    review['prompt_source_ids'] = sorted(sources)
    review['images'] = image_metadata
    review['image_errors'] = image_errors
    return review


def execute_task(task, dsn):
    profile = providers.Profile(**task['profile'])
    if task['stage'] == 'exchange':
        return peer_review(task)
    if task['stage'] != 'independent':
        raise ValueError('Unknown collaboration stage')
    with BraveQueryCache(dsn, refresh=task.get('refresh', False)) as cache:
        cache.context = {**task['context'], 'profile': profile.name}
        result = collaboration.collaborate_item(task['row'], task['bundle'], [profile],
            os.environ['BRAVE_SEARCH_API_KEY'], task['threshold'], task['context'], task.get('priors', []))
        result['brave_cache'] = cache.observations
        result['brave_api_requests'] = cache.requests
        result['brave_cache_hits'] = cache.hits
        return result


def worker(provider, dsn, url, stop=None):
    """One provider consumer; keep broker heartbeats alive during inference."""
    queue_name(provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        connection = connect_broker(url)
        store = Store(dsn)
        try:
            channel = connection.channel()
            declare(channel, provider=provider)
            channel.basic_qos(prefetch_count=1)
            for method, _, body in channel.consume(queue_name(provider), inactivity_timeout=1):
                if stop and stop.is_set():
                    break
                if method is None:
                    continue
                try:
                    task_id = UUID(json.loads(body)['task_id'])
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    channel.basic_reject(method.delivery_tag, requeue=False)
                    continue
                future = pool.submit(store.execute, task_id, provider, lambda task: execute_task(task, dsn))
                while not future.done():
                    connection.process_data_events(time_limit=.2)
                try:
                    reply = future.result()
                except ValueError:
                    channel.basic_reject(method.delivery_tag, requeue=False)
                    continue
                if reply:
                    import pika
                    try:
                        notification(channel, reply, task_id)
                    except pika.exceptions.UnroutableError:
                        deadline = store.conn.execute('SELECT deadline FROM enrichment.model_collaboration_tasks WHERE id=%s', (task_id,)).fetchone()['deadline']
                        if datetime.now(timezone.utc) < deadline:
                            raise
                        # The expired coordinator reply queue is gone. The durable
                        # result still exists, so discard this obsolete notification.
                channel.basic_ack(method.delivery_tag)
        finally:
            pool.shutdown(wait=True)
            store.close()
            if connection.is_open:
                connection.close()


def consume_loop(provider, dsn, url, stop):
    """Use the production reconnect path for transient quorum-queue startup races."""
    while not stop.is_set():
        try:
            worker(provider, dsn, url, stop)
        except Exception as exc:
            print(json.dumps({'event': 'collaboration_worker_error', 'provider': provider,
                              'error_type': type(exc).__name__}), flush=True)
            stop.wait(1)


def main():
    import argparse
    import signal
    from threading import Event
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', choices=PROVIDERS, required=True)
    args = parser.parse_args()
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    consume_loop(args.provider, os.environ['DATABASE_URL'], os.environ['RABBITMQ_URL'], stop)


def collaborate_item(dispatcher, row, bundle, profiles, threshold, context, priors=(), refresh=False):
    """Two barriers: independent work, then parallel review of pooled findings."""
    from copy import deepcopy
    from . import product_enrichment as core
    from . import product_comparison as comparison
    started = time.monotonic()
    inputs = {'row': row, 'bundle': bundle, 'threshold': threshold, 'priors': list(priors), 'refresh': refresh}
    independent = dispatcher.round('independent', profiles, inputs, context)
    errors, findings, cards = [], [], {}
    for profile, result in zip(profiles, independent):
        if result.get('status') == 'error':
            errors.append({'stage': 'independent_task', 'profile': profile.name, 'error_type': result.get('error_type')})
            continue
        errors.extend({**error, 'profile': profile.name} for error in result.get('blocking_errors', []))
        errors.extend({'stage': 'image', 'profile': profile.name, **error} for error in result.get('image_errors', []))
        if profile.vision and not result.get('images'):
            errors.append({'stage': 'image', 'profile': profile.name, 'error_type': 'MissingReceiptImages'})
        findings.append({'profile': profile.name, 'provider': profile.provider, 'model': profile.model,
                         'reviews': [{k: r.get(k) for k in ('provider', 'status', 'output')} for r in result.get('reviews', [])],
                         'proposals': [{k: r.get(k) for k in ('provider', 'status', 'output')} for r in result.get('proposals', [])]})
        for card in result.get('search_results', []):
            identifier = collaboration.candidate_id(card['url'])
            evidence = core.candidate_receipt_evidence(row['item_name'], core.retailer_domain(row['store_name']),
                                              card['title'], card['url'], card['snippet'], bundle['sources'])
            support = [s['id'] for s in bundle['sources'] if s['kind'] in {'ocr_pass', 'ocr_consensus', 'ocr_layout'} and
                       core.candidate_score(s['text'], core.retailer_domain(row['store_name']), card['title'], card['url'], card['snippet']) >= threshold]
            rebuilt = {**card, 'id': identifier, 'evidence': evidence, 'confidence': evidence['confidence'], 'ocr_support': support}
            # One canonical result per URL. Conflicting captured titles are kept
            # as an explicit error, rather than decided by worker arrival order.
            previous = cards.get(identifier)
            if previous and previous['title'] != rebuilt['title']:
                errors.append({'stage': 'pooled_evidence', 'error_type': 'ConflictingRetailerTitle', 'candidate_id': identifier})
            if not previous or rebuilt['confidence'] > previous['confidence']:
                cards[identifier] = rebuilt
    eligible = [c for c in cards.values() if c['evidence']['product_page'] and c['confidence'] >= threshold
                and c['ocr_support'] and all(t['kind'] != 'unmatched' for t in c['evidence']['tokens'])]
    eligible_ids = {c['id'] for c in eligible}
    candidates = sorted(eligible, key=lambda c: (-c['confidence'], c['id']))[:collaboration.MAX_REVIEW_CANDIDATES]
    if len(eligible) > len(candidates):
        errors.append({'stage': 'pooled_evidence', 'error_type': 'CandidateContextTruncated'})
    exchange = dispatcher.round('exchange', profiles, {**inputs, 'candidates': candidates, 'findings': findings}, context)
    reviews = []
    complete = (bundle['coverage']['available_passes'] == bundle['coverage']['included_passes'] and
                bundle['coverage']['available_documents'] == bundle['coverage']['included_documents'])
    image_sets = []
    for profile, result in zip(profiles, exchange):
        result = {**asdict(profile), 'profile': profile.name, **result}
        reviews.append(result)
        if result['status'] != 'success':
            errors.append({'stage': 'exchange', 'profile': profile.name, 'error_type': result.get('error_type')})
            complete = False
            continue
        coverage = result['exchange_coverage']
        complete = complete and coverage['included_sources'] == coverage['available_sources']
        if profile.vision:
            image_sets.append(result['images'])
            if result['image_errors'] or not result['images']:
                errors.append({'stage': 'exchange_image', 'profile': profile.name, 'error_type': 'MissingReceiptImages'})
    for profile, result in zip(profiles, independent):
        if profile.vision and result.get('images'):
            image_sets.append(result['images'])
    if image_sets and any(images != image_sets[0] for images in image_sets):
        errors.append({'stage': 'exchange_image', 'error_type': 'ReceiptImageEvidenceChanged'})
    min_families = min(2, len({p.provider for p in profiles}))
    # The literal-search quota is a discovery limit, not missing receipt evidence.
    # Once every final review completes with the full source bundle, reaching a
    # planned discovery quota is a warning even if the reviewers abstain. It
    # never supplies product evidence or agreement. Real failures remain blocking.
    # Conflicting titles on irrelevant, ineligible search hits are likewise
    # discovery warnings; conflicts on any eligible product remain blocking.
    discovery_warnings = [e for e in errors if (
        e.get('stage') == 'ocr_alternative_search' and e.get('error_type') == 'HypothesisBudgetExceeded') or (
        e.get('stage') == 'pooled_evidence' and e.get('error_type') == 'ConflictingRetailerTitle'
        and e.get('candidate_id') not in eligible_ids)]
    remaining_errors = [e for e in errors if e not in discovery_warnings]
    reviews_complete = len(reviews) == len(profiles) and all(r['status'] == 'success' for r in reviews)
    recovered_errors = discovery_warnings if complete and reviews_complete and not remaining_errors else []
    blocking_errors = remaining_errors if recovered_errors else errors
    decision = collaboration.reconcile(candidates, reviews, bundle['validations'], threshold, blocking_errors, complete,
                                        min_provider_families=min_families)
    if any(bundle['validations'].get(key) != 'pass' for key in ('receipt_arithmetic', 'item_arithmetic')):
        decision['disposition'] = 'review'
        decision['reasons'].append('receipt_arithmetic_not_verified')
    decision['review_policy'] = {'mode': 'rabbitmq_independent_then_exchange', 'skipped_profiles': [], 'fallback_reasons': []}
    payload = {**context, 'prompt_version': 'receipt-collaboration-v18-queued', 'scoring_version': core.SCORING_VERSION,
        'evidence_bundle': deepcopy(bundle), 'decision': decision, 'reviews': reviews,
        'independent_results': independent, 'independent_findings': findings,
        'candidates': candidates, 'search_results': list(cards.values()), 'available_candidates': len(cards),
        'errors': errors, 'blocking_errors': blocking_errors, 'recovered_errors': recovered_errors,
        'images': image_sets[0] if image_sets else [], 'image_errors': [e for e in errors if 'image' in e['stage']],
        'worker_identity': {'worker_host': socket.gethostname(), 'worker_node': os.getenv('K8S_NODE_NAME')},
        'item_seconds': round(time.monotonic() - started, 3),
        'brave_api_requests': sum(r.get('brave_api_requests', 0) for r in independent),
        'brave_cache_hits': sum(r.get('brave_cache_hits', 0) for r in independent)}
    for key in ('expansions', 'descriptor_expansions', 'proposals', 'search_queries', 'brave_cache', 'learned_searches'):
        payload[key] = [entry for result in independent for entry in result.get(key, [])]
    comparison.emit('enrichment_collaboration_decision', {**context, **decision,
        'item_seconds': payload['item_seconds'], 'blocking_error_count': len(blocking_errors), 'available_candidates': len(cards)})
    return payload
