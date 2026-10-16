"""KAN-98: rebuildable receipt evidence projection, independent of processing."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
from uuid import UUID, uuid4, uuid5
from urllib.parse import urlencode

VERSION = 8


def encoded(value):
    return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)


def identifier(kind, *parts):
    return kind + ':' + hashlib.sha256(encoded(parts).encode()).hexdigest()


def properties(values):
    """Neo4j properties are scalars; retain nested evidence as explicit JSON."""
    return {str(k): encoded(v) if isinstance(v, (dict, list, tuple)) else
            float(v) if isinstance(v, Decimal) else
            str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
            for k, v in values.items() if k not in {'data', 'raw_text'}}


class Graph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}

    def node(self, entity_kind, entity_key, display_label, **props):
        key = identifier(entity_kind, entity_key)
        prior = self.nodes.get(key, {})
        self.nodes[key] = {'id': key, 'kind': entity_kind, 'label': prior.get('label', str(display_label)),
                           'properties': {**prior.get('properties', {}), **properties(props)}}
        return key

    def edge(self, source, kind, target):
        assert re.fullmatch('[A-Z_]+', kind)
        key = identifier('edge', source, kind, target)
        self.edges[key] = {'id': key, 'source': source, 'target': target, 'kind': kind}

    def export(self):
        return {'version': VERSION, 'nodes': sorted(self.nodes.values(), key=lambda n: n['id']),
                'edges': sorted(self.edges.values(), key=lambda e: e['id'])}


def worker(graph, parent, identity):
    host = identity.get('host') or identity.get('worker_host') or identity.get('pod')
    node = identity.get('node') or identity.get('worker_node')
    if host:
        w = graph.node('Worker', host, host, **{k: v for k, v in identity.items()
                       if k in {'host', 'pod', 'worker_host', 'worker_pid', 'node', 'worker_node', 'service', 'pid'}})
        graph.edge(parent, 'EXECUTED_BY', w)
        if node and node != 'unknown':
            n = graph.node('Node', node, node)
            graph.edge(w, 'RUNS_ON', n)


def collaboration(graph, receipt, item, payload, skipped=(), completed_at=None, sequence=None, queued_tasks=()):
    scope = (payload['run_uuid'], payload['item_id'])
    run = graph.node('Collaboration', scope, 'Shared evidence', run_uuid=payload['run_uuid'], completed_at=completed_at, sequence=sequence,
                     item_seconds=payload.get('item_seconds'), prompt_version=payload.get('prompt_version'),
                     scoring_version=payload.get('scoring_version'), prompt_coverage=payload.get('prompt_coverage'),
                     discovery=payload.get('discovery'))
    graph.edge(receipt, 'HAS_COLLABORATION', run)
    graph.edge(run, 'ANALYZES', item)
    worker(graph, run, payload.get('worker_identity', {}))
    citations = {}
    bundle = payload.get('evidence_bundle', {})
    for source in bundle.get('sources', []):
        obs = graph.node('Observation', (*scope, source['id']), source.get('text', source['id']), **source)
        citations[source['id']] = obs
        graph.edge(run, 'USES_EVIDENCE', obs)
        if source.get('ocr_run_uuid') and source.get('pass_id'):
            p = graph.node('OCRPass', (source['ocr_run_uuid'], source['pass_id']), source.get('engine', 'OCR pass'))
            graph.edge(p, 'PRODUCED', obs)
    for image in payload.get('images', []):
        image = {k: v for k, v in image.items() if k != 'data'}
        ref = graph.node('ImageReference', (*scope, image['id']), f"Image page {image.get('page_number', image.get('page'))}", **image)
        citations[image['id']] = ref
        graph.edge(run, 'HAS_IMAGE_REFERENCE', ref)
    for candidate in payload.get('search_results', payload.get('candidates', [])):
        c = graph.node('SearchResult', (*scope, candidate['id']), candidate.get('title', 'Search result'), **candidate)
        citations[candidate['id']] = c
        page = graph.node('ProductPage', candidate['url'], candidate['url'], url=candidate['url'])
        graph.edge(c, 'REFERENCES', page)
        graph.edge(run, 'CONSIDERS', c)
        for source_id in candidate.get('ocr_support', []):
            if source_id in citations:
                graph.edge(citations[source_id], 'SUPPORTS', c)
    for query in payload.get('search_queries', []):
        query = dict(query)
        cached = next((entry for entry in payload.get('brave_cache', []) if entry['query'] == query['query']), None)
        if cached:
            query.update(cache_status=cached['cache_status'], fetched_at=cached['fetched_at'], cache_key=cached['cache_key'])
        q = graph.node('SearchQuery', (*scope, query['query']), query['query'],
                       **{key: query[key] for key in ('cache_status', 'fetched_at', 'cache_key') if key in query})
        graph.edge(run, 'SEARCHED', q)
        for c in query.get('candidate_ids', []):
            if c in citations:
                graph.edge(q, 'RETURNED', citations[c])
    searched = {entry['query'] for entry in payload.get('search_queries', [])}
    for learned in payload.get('learned_searches', []):
        if learned['query'] not in searched:
            continue
        query = graph.node('SearchQuery', (*scope, learned['query']), learned['query'])
        for prior in learned.get('prior_matches', []):
            match = graph.node('PriorProductMatch', (*scope, prior['cache_id']),
                               prior['product_title'], **prior)
            graph.edge(query, 'LEARNED_FROM', match)
            page = graph.node('ProductPage', prior['product_url'], prior['product_url'], url=prior['product_url'])
            graph.edge(match, 'REFERENCES', page)
    queued = 'independent_results' in payload
    task_nodes, round_nodes = {}, {}
    if queued:
        for phase, label in [('independent', 'Independent model findings'), ('exchange', 'Shared evidence review')]:
            round_nodes[phase] = graph.node('CollaborationRound', (*scope, phase), label,
                                           collaboration_round=phase, run_uuid=scope[0])
            graph.edge(run, 'HAS_ROUND', round_nodes[phase])
        for task in queued_tasks:
            phase, profile = task['stage'], task['profile']['name']
            if phase not in round_nodes:
                continue
            provenance = task.get('task_provenance') or {}
            t = graph.node('ModelTask', str(task['id']), f'{profile}: {phase} task', task_id=str(task['id']),
                cohort=str(task['cohort']), collaboration_round=phase, profile=profile, provider=task['provider'],
                status=task['status'], error_type=task.get('error_type'), attempts=task['attempts'], created_at=task['created_at'],
                completed_at=task['completed_at'], deadline=task['deadline'], run_uuid=scope[0],
                started_at=provenance.get('started_at'), worker_recorded=bool(provenance.get('worker_identity')))
            task_nodes[phase, profile] = t
            graph.edge(round_nodes[phase], 'DISPATCHED', t)
            route = graph.node('BrokerRoute', task['queue'], task['queue'], route=task['queue'])
            graph.edge(t, 'ROUTED_TO', route)
            worker(graph, t, provenance.get('worker_identity', {}))
        frames = []
        for result in payload['independent_results']:
            for key in ('descriptor_expansions', 'expansions', 'proposals', 'reviews'):
                frames.extend(({**r, 'collaboration_round': 'independent'}, result) for r in result.get(key, []))
        frames.extend(({**r, 'collaboration_round': 'exchange'}, payload)
                      for r in payload.get('reviews', []) if r.get('stage'))
    else:
        frames = [(r, payload) for r in payload.get('descriptor_expansions', []) + payload.get('expansions', []) +
                  payload.get('proposals', []) + payload.get('reviews', [])]
    proposals, shared_proposals, initial_reviews, final_reviews, exchange_invocations = [], [], {}, {}, []
    for invocation, invocation_payload in frames:
        phase = invocation['stage']
        profile = invocation['profile']
        invocation_scope = (*scope, profile, phase, invocation['target_reading']) if phase == 'expansion' else (*scope, profile, phase)
        if phase == 'descriptive_expansion':
            invocation_scope = (*scope, profile, phase, *invocation['descriptive_tokens'])
        if queued:
            invocation_scope = (*invocation_scope, invocation['collaboration_round'])
        i = graph.node('ModelInvocation', invocation_scope, f"{profile}: {phase}", **{
            'prompt_version': invocation_payload.get('prompt_version'),
            'scoring_version': invocation_payload.get('scoring_version'),
            **invocation, 'run_uuid': payload['run_uuid']})
        if queued:
            round_name = invocation['collaboration_round']
            graph.edge(round_nodes[round_name], 'HAS_INVOCATION', i)
            if (round_name, profile) in task_nodes:
                graph.edge(task_nodes[round_name, profile], 'HAS_INVOCATION', i)
            identity = invocation.get('task_provenance', {}).get('worker_identity', {})
            if round_name == 'independent':
                identity = identity or invocation_payload.get('worker_identity', {})
                if (round_name, profile) in task_nodes:
                    worker(graph, task_nodes[round_name, profile], identity)
                    graph.nodes[task_nodes[round_name, profile]]['properties']['worker_recorded'] = bool(identity)
            worker(graph, i, identity)
        model = graph.node('Model', (invocation['provider'], invocation['model'], invocation.get('model_digest')), invocation['model'],
                           provider=invocation['provider'], model=invocation['model'], digest=invocation.get('model_digest'))
        family = graph.node('Provider', invocation['provider'], invocation['provider'])
        graph.edge(run, 'HAS_INVOCATION', i)
        graph.edge(i, 'USES_MODEL', model)
        graph.edge(model, 'BELONGS_TO', family)
        if invocation.get('gpu_uuid'):
            gpu = graph.node('GPU', (invocation.get('gpu_host'), invocation['gpu_uuid']), invocation['gpu_uuid'],
                             host=invocation.get('gpu_host'), uuid=invocation['gpu_uuid'])
            graph.edge(i, 'OBSERVED_GPU', gpu)
            if invocation.get('gpu_host'):
                host = graph.node('InferenceHost', invocation['gpu_host'], invocation['gpu_host'])
                graph.edge(gpu, 'LOCATED_ON', host)
        output = invocation.get('output') or invocation.get('invalid_output') or {}
        valid = invocation.get('status') == 'success'
        outcome = graph.node('Proposal' if phase in {'proposal', 'expansion', 'descriptive_expansion'} else 'ModelReview', invocation_scope,
                             (output.get('candidate_title') if valid else None) or output.get('reading') or output.get('reason') or
                             next(iter(output.get('queries', [])), invocation['status']),
                             status=invocation['status'], valid=valid, output=output, profile=profile,
                             collaboration_round=invocation.get('collaboration_round'), provider=invocation['provider'])
        graph.edge(i, 'PRODUCED', outcome)
        if phase == 'review':
            if queued and invocation['collaboration_round'] == 'independent':
                initial_reviews[profile] = outcome
            else:
                final_reviews[profile] = outcome
                if queued:
                    exchange_invocations.append((profile, i, outcome))
        if queued:
            label = ('Initial review' if invocation['collaboration_round'] == 'independent' else 'Shared review') if phase == 'review' else phase
            graph.nodes[outcome]['label'] = f"{profile}: {label} — " + graph.nodes[outcome]['label']
        for source_id in invocation.get('derived_source_ids', []):
            if source_id in citations:
                graph.edge(outcome, 'DERIVED_FROM', citations[source_id])
        for query in invocation.get('searched_queries', []):
            if query in searched:
                q = graph.node('SearchQuery', (*scope, query), query)
                graph.edge(outcome, 'PROPOSED_SEARCH', q)
        cited_ids = list(output.get('source_ids', []))
        if output.get('product_source_id') and output['product_source_id'] not in cited_ids:
            cited_ids.append(output['product_source_id'])
        for source_id in cited_ids:
            target = citations.get(source_id)
            if target:
                graph.edge(outcome, 'CITES' if valid else 'REJECTED_CITATION', target)
            else:
                unknown = graph.node('UnresolvedCitation', (*invocation_scope, source_id), source_id)
                graph.edge(outcome, 'INVALID_CITATION', unknown)
        selected = output.get('candidate_id')
        if valid and selected in citations:
            graph.edge(outcome, 'SELECTS', citations[selected])
        if phase in {'proposal', 'expansion', 'descriptive_expansion'}:
            proposals.append(outcome)
            if queued and phase == 'proposal' and valid:
                shared_proposals.append(outcome)
        else:
            for peer in proposals:
                if (not queued or (invocation['collaboration_round'] == 'independent' and
                                   graph.nodes[peer]['properties'].get('profile') == profile) or
                                   (invocation['collaboration_round'] == 'exchange' and peer in shared_proposals)):
                    graph.edge(i, 'CONSIDERED_PROPOSAL', peer)
        for source in bundle.get('sources', []):
            # Prompt inclusion is bounded; do not claim every stored observation was seen.
            received = [] if phase == 'descriptive_expansion' else invocation.get('input_source_ids', []) if phase == 'expansion' else invocation.get('prompt_source_ids', invocation_payload.get('prompt_source_ids', []))
            if source['id'] in received:
                graph.edge(i, 'RECEIVED', citations[source['id']])
        if invocation.get('image_count', 0) > 0:
            for image in invocation_payload.get('images', [])[:invocation['image_count']]:
                graph.edge(i, 'RECEIVED', citations[image['id']])
    if queued:
        exchange = graph.node('EvidenceExchange', scope, 'Compare independent findings', run_uuid=scope[0])
        graph.edge(run, 'HAS_EXCHANGE', exchange)
        shared_outcomes = [n for n in initial_reviews.values() if graph.nodes[n]['properties']['valid']] + shared_proposals
        for outcome in shared_outcomes:
            graph.edge(outcome, 'SHARED_IN', exchange)
        for profile, invocation, outcome in exchange_invocations:
            graph.edge(exchange, 'SUPPLIED_TO', invocation)
            for initial in initial_reviews.values():
                if graph.nodes[initial]['properties']['valid']:
                    graph.edge(invocation, 'CONSIDERED_REVIEW', initial)
            if profile in initial_reviews:
                initial = initial_reviews[profile]
                before = json.loads(graph.nodes[initial]['properties']['output']).get('candidate_id')
                after = json.loads(graph.nodes[outcome]['properties']['output']).get('candidate_id')
                revised = graph.nodes[initial]['properties']['valid'] and graph.nodes[outcome]['properties']['valid'] and before != after
                graph.edge(outcome, 'REVISES' if revised else 'FOLLOWS', initial)
    for profile in skipped:
        n = graph.node('SkippedProvider', (*scope, encoded(profile)), 'Unavailable provider', **profile)
        graph.edge(run, 'SKIPPED', n)
    decision = payload['decision']
    d = graph.node('Decision', scope, decision['disposition'], **decision)
    graph.edge(run, 'RESULTED_IN', d)
    for review in final_reviews.values():
        graph.edge(review, 'INFORMS', d)
    for name, result in bundle.get('validations', {}).items():
        guard = graph.node('Guardrail', (*scope, name), name, status=result)
        graph.edge(d, 'EVALUATED', guard)
    if decision.get('candidate_id') in citations:
        graph.edge(d, 'RECOMMENDS' if decision['disposition'] == 'recommended' else 'CONSIDERS', citations[decision['candidate_id']])
    # There is deliberately no ACCEPTED/PERSISTED edge from a model recommendation.


def build(data):
    graph = Graph()
    evidence = data['receipt']
    sha = evidence['source_sha256']
    receipt = graph.node('Receipt', sha, evidence['source_reference'], **evidence)
    expense = data.get('expense')
    items = {}
    if expense:
        canonical = graph.node('CanonicalReceipt', (sha, expense['id']), expense.get('store_name') or 'Canonical receipt', **expense)
        record = graph.node('PostgreSQLRecord', ('budget.expenses', sha, expense['id']), f"budget.expenses/{expense['id']}", table='budget.expenses', key=expense['id'])
        graph.edge(receipt, 'PARSED_AS', canonical)
        graph.edge(canonical, 'PERSISTED_AS', record)
        if expense.get('store_name'):
            merchant = graph.node('Merchant', expense['store_name'], expense['store_name'])
            graph.edge(canonical, 'PURCHASED_FROM', merchant)
        for item in data.get('items', []):
            i = graph.node('Item', (sha, item['id']), item['item_name'], **item)
            items[item['id']] = i
            graph.edge(canonical, 'CONTAINS', i)
            if item.get('budget_category'):
                c = graph.node('Category', item['budget_category'], item['budget_category'])
                graph.edge(i, 'CLASSIFIED_AS', c)
    for run in data.get('ocr_runs', []):
        attempt = graph.node('OCRRun', str(run['run_uuid']), 'OCR run', **run)
        graph.edge(receipt, 'HAS_OCR_RUN', attempt)
        extraction = graph.node('ExtractionResult', str(run['run_uuid']), run.get('extraction_status') or 'Unknown extraction outcome',
                                status=run.get('extraction_status'), confidence=run.get('extraction_confidence'),
                                evidence_id=run.get('evidence_id'), processing_seconds=run.get('processing_seconds'))
        graph.edge(attempt, 'PRODUCED', extraction)
        worker(graph, attempt, {**run.get('worker_identity', {}), 'worker_host': run.get('worker_host')})
        traceparent = run.get('traceparent') or ''
        if re.fullmatch(r'[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}', traceparent):
            trace = graph.node('Trace', traceparent.split('-')[1], traceparent.split('-')[1], trace_id=traceparent.split('-')[1])
            graph.edge(attempt, 'TRACED_BY', trace)
    for p in data.get('ocr_passes', []):
        run_id = identifier('OCRRun', str(p['run_uuid']))
        invocation = graph.node('OCRPass', (str(p['run_uuid']), p['pass_id']), p['engine'], **p)
        graph.edge(run_id, 'HAS_PASS', invocation)
        model = graph.node('OCRMethod', (p['engine'], encoded(p.get('engine_options', {}))), p['engine'], options=p.get('engine_options'))
        graph.edge(invocation, 'USES_METHOD', model)
    previous_retry = {}
    for event in data.get('events', []):
        e = event['payload']
        if e.get('event') in {'product_recommendation_accepted', 'receipt_item_refreshed'}:
            continue
        message = graph.node('Message', e['message_id'], e['operation'], message_id=e['message_id'], request_id=e.get('request_id'), operation=e['operation'], batch_id=e.get('batch_id'))
        graph.edge(receipt, 'QUEUED_AS', message)
        if e.get('queue'):
            route = graph.node('BrokerRoute', e['queue'], e['queue'], route=e['queue'])
            graph.edge(message, 'ROUTED_TO', route)
        if not e.get('attempt_id'):
            publication = graph.node('MessageEvent', str(event['id']), e['status'], occurred_at=event['occurred_at'], **e)
            graph.edge(message, 'OBSERVED', publication)
            continue
        attempt = graph.node('ProcessingAttempt', e['attempt_id'], f"Attempt {e['attempt']}", attempt_id=e['attempt_id'], operation=e['operation'], attempt=e['attempt'])
        graph.edge(message, 'HAS_ATTEMPT', attempt)
        worker(graph, attempt, e)
        observation = graph.node('AttemptEvent', str(event['id']), e['status'], occurred_at=event['occurred_at'], **e)
        graph.edge(attempt, 'OBSERVED', observation)
        for run_uuid in e.get('result_runs', []):
            result = graph.node('OCRRun', run_uuid, 'OCR run', run_uuid=run_uuid)
            graph.edge(attempt, 'PRODUCED', result)
        if e['status'] == 'active' and e['message_id'] in previous_retry:
            graph.edge(previous_retry.pop(e['message_id']), 'RETRIED_AS', attempt)
        if e['status'] == 'retried':
            previous_retry[e['message_id']] = attempt
    for event in data.get('result_events', []):
        result = graph.node('ResultMessage', event['message_id'], event['event_type'], **event)
        run = graph.node('OCRRun', str(event['run_uuid']), 'OCR run', run_uuid=str(event['run_uuid']))
        graph.edge(run, 'PRODUCED', result)
        record = graph.node('PostgreSQLRecord', ('budget.ocr_result_events', event['message_id']),
                            'Persisted OCR result event', table='budget.ocr_result_events', key=event['message_id'], persisted_at=event['persisted_at'])
        graph.edge(result, 'PERSISTED_AS', record)
    for row in data.get('collaborations', []):
        p = row['payload']
        if p['item_id'] in items:
            collaboration(graph, receipt, items[p['item_id']], p, row.get('summary', {}).get('skipped_profiles', []), row.get('completed_at'), row.get('id'), row.get('queued_tasks', []))
    for event in data.get('events', []):
        e = event['payload']
        if e.get('event') == 'receipt_item_refreshed':
            if e.get('item_id') in items:
                refresh = graph.node('ItemRefresh', str(event['id']), 'Accepted matches and category rules applied',
                                     occurred_at=event['occurred_at'], **e)
                graph.edge(refresh, 'UPDATED', items[e['item_id']])
                record = graph.node('PostgreSQLRecord', ('lineage.receipt_events', str(event['id'])),
                                    'Audited item refresh', table='lineage.receipt_events', key=str(event['id']))
                graph.edge(refresh, 'PERSISTED_AS', record)
            continue
        if e.get('event') != 'product_recommendation_accepted' or e.get('item_id') not in items:
            continue
        decision = identifier('Decision', (e['run_uuid'], e['item_id']))
        if decision not in graph.nodes:
            continue
        acceptance = graph.node('ProductAcceptance', str(event['id']), 'Accepted recommendation',
                                accepted_at=event['occurred_at'], **e)
        graph.edge(decision, 'ACCEPTED_AS', acceptance)
        graph.edge(acceptance, 'UPDATED', items[e['item_id']])
        audit = graph.node('PostgreSQLRecord', ('budget.expense_item_description_audit', e['audit_id']),
                           'Audited product acceptance', table='budget.expense_item_description_audit', key=e['audit_id'])
        graph.edge(acceptance, 'PERSISTED_AS', audit)
    return graph.export()


def load_collaborations(conn, expense_id):
    """Stream one payload at a time in the caller's consistent transaction."""
    if expense_id is None:
        return
    task_table = conn.execute("SELECT to_regclass('enrichment.model_collaboration_tasks') AS name").fetchone()['name']
    # The default client cursor buffers every JSON payload. Long receipts have
    # hundreds of MB of repeated evidence; even a server cursor must fetch one
    # row per batch to keep that history from exhausting the projector's memory.
    with conn.cursor(name='receipt_graph_' + uuid4().hex) as cursor:
        cursor.itersize = 1
        cursor.execute('''SELECT c.payload,c.completed_at,c.id,COALESCE(r.summary,'{}'::jsonb) AS summary
            FROM enrichment.receipt_collaborations c
            LEFT JOIN enrichment.receipt_collaboration_runs r USING(run_uuid)
            JOIN budget.expense_items i ON i.id=c.expense_item_id
            WHERE i.expense_pk=%s ORDER BY c.completed_at,c.id''', (expense_id,))
        for row in cursor:
            p = row['payload']
            if 'independent_results' in p:
                row['queued_tasks'] = []
                cohorts = [uuid5(UUID(p['run_uuid']), f"{p['item_id']}:{phase}") for phase in ('independent', 'exchange')]
                if task_table:
                    tasks = conn.execute('''SELECT id,cohort,provider,attempts,deadline,created_at,completed_at,
                        input->>'stage' AS stage,input->'profile' AS profile,input->>'reply' AS reply,
                        result->'task_provenance' AS task_provenance,result->>'error_type' AS error_type,
                        CASE WHEN result IS NULL THEN 'pending' WHEN result->>'status'='error' THEN 'error' ELSE 'completed' END AS status
                        FROM enrichment.model_collaboration_tasks WHERE cohort=ANY(%s) ORDER BY created_at,id''', (cohorts,)).fetchall()
                    for task in tasks:
                        task['queue'] = task.pop('reply').split('.results.')[0] + '.' + task['provider']
                    row['queued_tasks'] = tasks
            yield row


def load(conn, sha):
    """Read one consistent authoritative snapshot; no graph or model calls."""
    from .receipts.catalogue import RECEIPT_CATALOGUE
    receipt = conn.execute(f'''SELECT ir.*,r.*,s.status,s.attempts FROM {RECEIPT_CATALOGUE} r
        LEFT JOIN ingest.receipts ir USING(source_sha256)
        LEFT JOIN ingest.receipt_processing_status s USING(source_sha256)
        WHERE r.source_sha256=%s''', (sha,)).fetchone()
    if not receipt:
        raise ValueError('Receipt not found')
    expense = conn.execute('SELECT x.* FROM budget.expenses x JOIN budget.receipt_evidence e ON e.expense_pk=x.id WHERE e.source_sha256=%s ORDER BY e.is_primary_source DESC,e.id LIMIT 1', (sha,)).fetchone()
    runs = conn.execute('SELECT * FROM budget.receipt_ocr_runs WHERE source_sha256=%s ORDER BY processed_at,run_uuid', (sha,)).fetchall()
    passes = conn.execute('SELECT p.* FROM budget.receipt_ocr_passes p JOIN budget.receipt_ocr_runs r USING(run_uuid) WHERE r.source_sha256=%s ORDER BY r.processed_at,p.pass_id', (sha,)).fetchall()
    events = conn.execute('SELECT * FROM lineage.receipt_events WHERE source_sha256=%s ORDER BY occurred_at,id', (sha,)).fetchall()
    result_events = conn.execute('SELECT * FROM budget.ocr_result_events WHERE source_sha256=%s ORDER BY persisted_at,message_id', (sha,)).fetchall()
    items = conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s ORDER BY id', (expense['id'],)).fetchall() if expense else []
    collaborations = load_collaborations(conn, expense['id'] if expense else None)
    return {'receipt': receipt, 'expense': expense, 'items': items, 'ocr_runs': runs, 'ocr_passes': passes, 'events': events,
            'result_events': result_events, 'collaborations': collaborations}


def driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(os.environ['NEO4J_URI'], auth=(os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD']),
                               connection_timeout=3, max_transaction_retry_time=5, connection_acquisition_timeout=5)


def write_batches(rows, *, max_rows=500, max_chars=256 * 1024):
    """Bound serialized parameter copies by text size as well as row count.

    A single oversized entity is sent alone; its evidence is never discarded.
    Rows contain only scalar values, including pre-serialized JSON strings.
    """
    batch, chars = [], 0
    for row in rows:
        size = sum(len(v) if isinstance(v, str) else 32 for v in row.values())
        if batch and (len(batch) >= max_rows or chars + size > max_chars):
            yield batch
            batch, chars = [], 0
        batch.append(row)
        chars += size
    if batch:
        yield batch


def write(tx, sha, graph, observed_at):
    """A per-receipt lock prevents stale snapshots racing newer projections."""
    tx.run('MERGE (s:ReceiptSnapshot {sha:$sha}) SET s.lock=coalesce(s.lock,0)+1', sha=sha).consume()
    prior = tx.run('MATCH (s:ReceiptSnapshot {sha:$sha}) RETURN s.observed_at AS time', sha=sha).single()
    if prior['time'] and prior['time'] > observed_at:
        return False
    rows = ({'id': n['id'], 'kind': n['kind'], 'label': n['label'],
             'props': encoded(n['properties'])} for n in graph['nodes'])
    for batch in write_batches(rows):
        tx.run('UNWIND $nodes AS row MERGE (n:ReceiptFlowEntity {id:row.id}) SET n.kind=row.kind,n.label=row.label,n.properties_json=row.props',
               nodes=batch).consume()
    # Relationship types come exclusively from our validated graph builder.
    grouped = defaultdict(list)
    outgoing = defaultdict(list)
    item_for = {}
    for edge in graph['edges']:
        if not re.fullmatch('[A-Z_]+', edge['kind']):
            raise ValueError('Invalid relationship type')
        grouped[edge['kind']].append(edge)
        outgoing[edge['source']].append(edge)
        if edge['kind'] == 'ANALYZES':
            item_for[edge['source']] = edge['target']
    tx.run('MATCH (s:ReceiptSnapshot {sha:$sha})-[r:HAS_ENTITY]->() DELETE r', sha=sha).consume()
    # Store exact snapshot properties per membership. Stream serialization so
    # hundreds of large evidence records do not share one Bolt parameter batch.
    members = ({
        'id': n['id'], 'kind': n['kind'], 'label': n['label'][:256],
        'story': encoded({k: n['properties'][k] for k in
            ('run_uuid', 'completed_at', 'sequence') if k in n['properties']}),
        'payload': encoded(n), 'ordinal': i,
        'edges': encoded(outgoing[n['id']]),
        'item': item_for.get(n['id']),
        'edge_ids': [e['id'] for e in outgoing[n['id']]],
    } for i, n in enumerate(graph['nodes']))
    for batch in write_batches(members):
        tx.run('''MATCH (s:ReceiptSnapshot {sha:$sha}) UNWIND $members AS row
            MATCH (n:ReceiptFlowEntity {id:row.id}) MERGE (s)-[r:HAS_ENTITY]->(n)
            SET r.node_id=row.id,r.kind=row.kind,r.label=row.label,
                r.story_json=row.story,r.node_json=row.payload,r.ordinal=row.ordinal,
                r.edges_json=row.edges,r.item_id=row.item,r.edge_ids=row.edge_ids''',
            sha=sha, members=batch).consume()
    for kind, rows in grouped.items():
        for offset in range(0, len(rows), 1000):
            tx.run(f'UNWIND $rows AS row MATCH (a:ReceiptFlowEntity {{id:row.source}}),(b:ReceiptFlowEntity {{id:row.target}}) MERGE (a)-[r:{kind} {{id:row.id}}]->(b)', rows=rows[offset:offset + 1000]).consume()
    # Web v7 reads per-node snapshot memberships, never the legacy monolithic
    # graph_json. Avoid allocating another receipt-sized JSON string during writes.
    tx.run('MATCH (s:ReceiptSnapshot {sha:$sha}) SET s.observed_at=$time,s.version=$version,s.name=$name,s.edges_json=$edges REMOVE s.graph_json',
           sha=sha, edges=encoded(graph['edges']), time=observed_at, version=VERSION,
           name=next(n['label'] for n in graph['nodes'] if n['kind'] == 'Receipt')).consume()
    return True


def project(dsn, *, sha=None, limit=100, after=''):
    import psycopg
    from psycopg.rows import dict_row
    from .receipts.catalogue import RECEIPT_CATALOGUE
    with driver() as backend, psycopg.connect(dsn, row_factory=dict_row) as conn:
        with backend.session(database=os.getenv('NEO4J_DATABASE', 'neo4j')) as session:
            session.run('CREATE CONSTRAINT receipt_flow_id IF NOT EXISTS FOR (n:ReceiptFlowEntity) REQUIRE n.id IS UNIQUE').consume()
            session.run('CREATE CONSTRAINT receipt_snapshot_sha IF NOT EXISTS FOR (n:ReceiptSnapshot) REQUIRE n.sha IS UNIQUE').consume()
            rows = conn.execute(f'SELECT source_sha256 FROM {RECEIPT_CATALOGUE} r WHERE (%s OR source_sha256=%s) AND source_sha256>%s ORDER BY source_sha256 LIMIT %s', (sha is None, sha, after, limit)).fetchall()
            conn.commit()
            for row in rows:
                print(encoded({'event': 'receipt_graph_projection_started', 'sha': row['source_sha256']}), flush=True)
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                observed = conn.execute('SELECT transaction_timestamp() AS time').fetchone()['time'].astimezone(timezone.utc).isoformat(timespec='microseconds')
                graph = build(load(conn, row['source_sha256']))
                conn.commit()
                session.execute_write(write, row['source_sha256'], graph, observed)
                print(encoded({'event': 'receipt_graph_projection_completed', 'sha': row['source_sha256'],
                               'nodes': len(graph['nodes']), 'edges': len(graph['edges'])}), flush=True)
                # Assignment alone retains the prior graph while building the next.
                del graph
    return {'projected': len(rows), 'after': rows[-1]['source_sha256'] if rows else after}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--after', default='')
    parser.add_argument('--all', action='store_true', help='Replay every receipt in bounded batches')
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 1000 or (args.sha and not re.fullmatch('[0-9a-f]{64}', args.sha)):
        parser.error('Use a valid SHA and LIMIT from 1 to 1000')
    try:
        after = args.after
        while True:
            result = project(os.environ['DATABASE_URL'], sha=args.sha, limit=args.limit, after=after)
            print(encoded(result), flush=True)
            after = result['after']
            if not args.all or args.sha or result['projected'] < args.limit:
                break
    except Exception as exc:
        print(encoded({'event': 'receipt_graph_projection_failed', 'error_type': type(exc).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
