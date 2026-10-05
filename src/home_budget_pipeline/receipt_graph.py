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
from urllib.parse import urlencode

VERSION = 1


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


def collaboration(graph, receipt, item, payload, skipped=()):
    scope = (payload['run_uuid'], payload['item_id'])
    run = graph.node('Collaboration', scope, 'Shared evidence', run_uuid=payload['run_uuid'],
                     item_seconds=payload.get('item_seconds'), prompt_version=payload.get('prompt_version'),
                     scoring_version=payload.get('scoring_version'), prompt_coverage=payload.get('prompt_coverage'))
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
        q = graph.node('SearchQuery', (*scope, query['query']), query['query'])
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
    proposals = []
    for invocation in payload.get('expansions', []) + payload.get('proposals', []) + payload.get('reviews', []):
        phase = invocation['stage']
        profile = invocation['profile']
        invocation_scope = (*scope, profile, phase, invocation['target_reading']) if phase == 'expansion' else (*scope, profile, phase)
        i = graph.node('ModelInvocation', invocation_scope, f"{profile}: {phase}", **{**invocation, 'run_uuid': payload['run_uuid']})
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
        outcome = graph.node('Proposal' if phase in {'proposal', 'expansion'} else 'ModelReview', invocation_scope,
                             output.get('reading') or output.get('reason') or invocation['status'],
                             status=invocation['status'], valid=valid, output=output)
        graph.edge(i, 'PRODUCED', outcome)
        for query in invocation.get('searched_queries', []):
            if query in searched:
                q = graph.node('SearchQuery', (*scope, query), query)
                graph.edge(outcome, 'PROPOSED_SEARCH', q)
        for source_id in output.get('source_ids', []):
            target = citations.get(source_id)
            if target:
                graph.edge(outcome, 'CITES' if valid else 'REJECTED_CITATION', target)
            else:
                unknown = graph.node('UnresolvedCitation', (*invocation_scope, source_id), source_id)
                graph.edge(outcome, 'INVALID_CITATION', unknown)
        selected = output.get('candidate_id')
        if valid and selected in citations:
            graph.edge(outcome, 'SELECTS', citations[selected])
        if phase in {'proposal', 'expansion'}:
            proposals.append(outcome)
        else:
            for peer in proposals:
                graph.edge(i, 'CONSIDERED_PROPOSAL', peer)
        for source in bundle.get('sources', []):
            # Prompt inclusion is bounded; do not claim every stored observation was seen.
            received = invocation.get('input_source_ids', []) if phase == 'expansion' else payload.get('prompt_source_ids', [])
            if source['id'] in received:
                graph.edge(i, 'RECEIVED', citations[source['id']])
        if invocation.get('image_count', 0) > 0:
            for image in payload.get('images', [])[:invocation['image_count']]:
                graph.edge(i, 'RECEIVED', citations[image['id']])
    for profile in skipped:
        n = graph.node('SkippedProvider', (*scope, encoded(profile)), 'Unavailable provider', **profile)
        graph.edge(run, 'SKIPPED', n)
    decision = payload['decision']
    d = graph.node('Decision', scope, decision['disposition'], **decision)
    graph.edge(run, 'RESULTED_IN', d)
    for review in payload.get('reviews', []):
        graph.edge(graph.nodes[identifier('ModelReview', (*scope, review['profile'], review['stage']))]['id'], 'INFORMS', d)
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
            collaboration(graph, receipt, items[p['item_id']], p, row.get('summary', {}).get('skipped_profiles', []))
    return graph.export()


def load(conn, sha):
    """Read one consistent authoritative snapshot; no graph or model calls."""
    receipt = conn.execute('SELECT r.*,s.status,s.attempts FROM ingest.receipts r LEFT JOIN ingest.receipt_processing_status s USING(source_sha256) WHERE r.source_sha256=%s', (sha,)).fetchone()
    if not receipt:
        raise ValueError('Receipt not found')
    expense = conn.execute('SELECT x.* FROM budget.expenses x JOIN budget.receipt_evidence e ON e.expense_pk=x.id WHERE e.source_sha256=%s ORDER BY e.is_primary_source DESC,e.id LIMIT 1', (sha,)).fetchone()
    runs = conn.execute('SELECT * FROM budget.receipt_ocr_runs WHERE source_sha256=%s ORDER BY processed_at,run_uuid', (sha,)).fetchall()
    passes = conn.execute('SELECT p.* FROM budget.receipt_ocr_passes p JOIN budget.receipt_ocr_runs r USING(run_uuid) WHERE r.source_sha256=%s ORDER BY r.processed_at,p.pass_id', (sha,)).fetchall()
    events = conn.execute('SELECT * FROM lineage.receipt_events WHERE source_sha256=%s ORDER BY occurred_at,id', (sha,)).fetchall()
    result_events = conn.execute('SELECT * FROM budget.ocr_result_events WHERE source_sha256=%s ORDER BY persisted_at,message_id', (sha,)).fetchall()
    items = conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s ORDER BY id', (expense['id'],)).fetchall() if expense else []
    collaborations = conn.execute('SELECT c.payload,COALESCE(r.summary,\'{}\'::jsonb) AS summary FROM enrichment.receipt_collaborations c LEFT JOIN enrichment.receipt_collaboration_runs r USING(run_uuid) JOIN budget.expense_items i ON i.id=c.expense_item_id WHERE i.expense_pk=%s ORDER BY c.completed_at,c.id', (expense['id'],)).fetchall() if expense else []
    return {'receipt': receipt, 'expense': expense, 'items': items, 'ocr_runs': runs, 'ocr_passes': passes, 'events': events,
            'result_events': result_events, 'collaborations': collaborations}


def driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(os.environ['NEO4J_URI'], auth=(os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD']),
                               connection_timeout=3, max_transaction_retry_time=5, connection_acquisition_timeout=5)


def write(tx, sha, graph, observed_at):
    """A per-receipt lock prevents stale snapshots racing newer projections."""
    tx.run('MERGE (s:ReceiptSnapshot {sha:$sha}) SET s.lock=coalesce(s.lock,0)+1', sha=sha).consume()
    prior = tx.run('MATCH (s:ReceiptSnapshot {sha:$sha}) RETURN s.observed_at AS time', sha=sha).single()
    if prior['time'] and prior['time'] > observed_at:
        return False
    tx.run('UNWIND $nodes AS row MERGE (n:ReceiptFlowEntity {id:row.id}) SET n.kind=row.kind,n.label=row.label,n.properties_json=row.props',
           nodes=[{**n, 'props': encoded(n['properties'])} for n in graph['nodes']]).consume()
    # Relationship types come exclusively from our validated graph builder.
    grouped = defaultdict(list)
    for edge in graph['edges']:
        if not re.fullmatch('[A-Z_]+', edge['kind']):
            raise ValueError('Invalid relationship type')
        grouped[edge['kind']].append(edge)
    tx.run('MATCH (s:ReceiptSnapshot {sha:$sha})-[r:HAS_ENTITY]->() DELETE r', sha=sha).consume()
    tx.run('MATCH (s:ReceiptSnapshot {sha:$sha}) UNWIND $ids AS id MATCH (n:ReceiptFlowEntity {id:id}) MERGE (s)-[:HAS_ENTITY]->(n)',
           sha=sha, ids=[n['id'] for n in graph['nodes']]).consume()
    for kind, rows in grouped.items():
        tx.run(f'UNWIND $rows AS row MATCH (a:ReceiptFlowEntity {{id:row.source}}),(b:ReceiptFlowEntity {{id:row.target}}) MERGE (a)-[r:{kind} {{id:row.id}}]->(b)', rows=rows).consume()
    # UI reads the atomic snapshot, never a mix of current and historical states.
    tx.run('MATCH (s:ReceiptSnapshot {sha:$sha}) SET s.graph_json=$graph,s.observed_at=$time,s.version=$version,s.name=$name',
           sha=sha, graph=encoded(graph), time=observed_at, version=VERSION,
           name=next(n['label'] for n in graph['nodes'] if n['kind'] == 'Receipt')).consume()
    return True


def project(dsn, *, sha=None, limit=100, after=''):
    import psycopg
    from psycopg.rows import dict_row
    with driver() as backend, psycopg.connect(dsn, row_factory=dict_row) as conn:
        with backend.session(database=os.getenv('NEO4J_DATABASE', 'neo4j')) as session:
            session.run('CREATE CONSTRAINT receipt_flow_id IF NOT EXISTS FOR (n:ReceiptFlowEntity) REQUIRE n.id IS UNIQUE').consume()
            session.run('CREATE CONSTRAINT receipt_snapshot_sha IF NOT EXISTS FOR (n:ReceiptSnapshot) REQUIRE n.sha IS UNIQUE').consume()
            rows = conn.execute('SELECT source_sha256 FROM ingest.receipts WHERE (%s OR source_sha256=%s) AND source_sha256>%s ORDER BY source_sha256 LIMIT %s', (sha is None, sha, after, limit)).fetchall()
            conn.commit()
            for row in rows:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                observed = conn.execute('SELECT transaction_timestamp() AS time').fetchone()['time'].astimezone(timezone.utc).isoformat(timespec='microseconds')
                graph = build(load(conn, row['source_sha256']))
                conn.commit()
                session.execute_write(write, row['source_sha256'], graph, observed)
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
