"""Authenticated graph navigation; database credentials stay on the server."""
from datetime import datetime, timezone
import json
import os
import ssl
from pathlib import Path
from urllib.parse import urlencode, urlparse
import urllib.request

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from .app import app, BASE_PATH, authenticated_identity
from .render import page
from .. import receipt_graph as graph

PERSPECTIVES = {
    'story': None,
    'lineage': None,
    'topology': {'Receipt', 'Message', 'MessageEvent', 'BrokerRoute', 'ResultMessage', 'ProcessingAttempt', 'AttemptEvent', 'Worker', 'Node', 'OCRRun', 'OCRPass', 'OCRMethod', 'Model', 'ModelInvocation', 'Provider', 'GPU', 'InferenceHost', 'ExtractionResult', 'Guardrail', 'Decision', 'ProductAcceptance', 'Collaboration', 'PostgreSQLRecord'},
    'domain': {'Receipt', 'CanonicalReceipt', 'Merchant', 'Item', 'Category', 'ProductPage', 'SearchResult', 'Decision', 'ProductAcceptance', 'Collaboration'},
}


def read(query, **params):
    if not os.getenv('NEO4J_URI'):
        raise HTTPException(503, 'Receipt graph is not configured')
    try:
        with graph.driver() as driver, driver.session(database=os.getenv('NEO4J_DATABASE', 'neo4j')) as session:
            return session.execute_read(lambda tx: [r.data() for r in tx.run(query, **params)])
    except Exception:
        raise HTTPException(503, 'Receipt graph is unavailable; receipt processing continues') from None


def links(node):
    props = node['properties']
    result = []
    base = os.getenv('GRAPH_GRAFANA_URL', 'https://grafana.idc.brownrook.net').rstrip('/')
    if urlparse(base).scheme not in {'https', 'http'}:
        return result
    for label, datasource, query in (
        ('Loki logs', os.getenv('GRAPH_LOKI_UID', 'cfy4qg8i178jkb'), '{namespace="home-budget"}' +
         (' |= ' + json.dumps(str(props.get('run_uuid') or props.get('worker_host') or props.get('message_id') or props.get('source_sha256'))) if any(props.get(k) for k in ('run_uuid', 'worker_host', 'message_id', 'source_sha256')) else '')),
        ('Tempo trace', os.getenv('GRAPH_TEMPO_UID', 'home-budget-tempo'), props.get('trace_id')),
    ):
        if query:
            panes = {'A': {'datasource': datasource, 'queries': [{'refId': 'A', 'datasource': {'uid': datasource},
                      'expr': query, 'query': query, 'queryType': 'traceId' if label == 'Tempo trace' else 'range'}],
                      'range': {'from': 'now-24h', 'to': 'now'}}}
            result.append({'label': label, 'url': base + '/explore?' + urlencode({'schemaVersion': 1, 'panes': json.dumps(panes)})})
    result.append({'label': 'Grafana metrics', 'url': base})
    if node['kind'] == 'PostgreSQLRecord' and props.get('table') == 'budget.expenses':
        result.append({'label': 'Canonical receipt', 'url': f"{BASE_PATH}/expenses/{int(props['key'])}"})
    url = props.get('url')
    if isinstance(url, str) and urlparse(url).scheme in {'http', 'https'}:
        result.append({'label': 'Product evidence', 'url': url})
    return result


@app.get(f'{BASE_PATH}/api/graph/receipts')
def receipts(q: str = Query('', max_length=200), _: dict = Depends(authenticated_identity)):
    return read('MATCH (s:ReceiptSnapshot) WHERE toLower(s.name) CONTAINS toLower($q) OR s.sha=$q RETURN s.sha AS sha,s.name AS name,s.observed_at AS observed_at ORDER BY s.name LIMIT 100', q=q)


@app.get(f'{BASE_PATH}/api/graph/entities/{{entity}}/receipts')
def related(entity: str, _: dict = Depends(authenticated_identity)):
    if len(entity) > 150:
        raise HTTPException(422, 'Invalid entity')
    return read('MATCH (s:ReceiptSnapshot)-[:HAS_ENTITY]->(n:ReceiptFlowEntity {id:$id}) RETURN s.sha AS sha,s.name AS name,s.observed_at AS observed_at ORDER BY s.name LIMIT 100', id=entity)


def kubernetes_health():
    root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    if not (root / 'token').exists():
        return []
    namespace = (root / 'namespace').read_text().strip()
    host = os.getenv('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
    port = os.getenv('KUBERNETES_SERVICE_PORT_HTTPS', '443')
    request = urllib.request.Request(f'https://{host}:{port}/api/v1/namespaces/{namespace}/pods',
                                    headers={'Authorization': 'Bearer ' + (root / 'token').read_text().strip()})
    with urllib.request.urlopen(request, timeout=3, context=ssl.create_default_context(cafile=str(root / 'ca.crt'))) as response:
        rows = json.load(response)['items']
    return [{'pod': p['metadata']['name'], 'node': p['spec'].get('nodeName'),
             'status': 'healthy' if any(c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', [])) else 'unhealthy',
             'phase': p['status'].get('phase'), 'app': p['metadata'].get('labels', {}).get('app')}
            for p in rows if p['status'].get('phase') not in {'Succeeded', 'Failed'}]


def processing_story(result, item_id='', run_id=''):
    """Scope the whole snapshot before display; historical runs cannot mix evidence."""
    nodes = {n['id']: n for n in result['nodes']}
    edges = result['edges']
    runs = [n for n in nodes.values() if n['kind'] == 'Collaboration']
    item_for = {e['source']: e['target'] for e in edges if e['kind'] == 'ANALYZES'}
    items = [nodes[key] for key in dict.fromkeys(item_for.values()) if key in nodes]
    if item_id and item_id not in {n['id'] for n in items}:
        raise HTTPException(422, 'Item is not part of this receipt collaboration')
    # SQL completion time and row ID are authoritative, never UUID lexical order.
    runs.sort(key=lambda n: (str(n['properties'].get('completed_at') or ''),
                             n['properties'].get('sequence') or 0), reverse=True)
    selected_item = item_id or (item_for.get(runs[0]['id'], '') if runs else '')
    available = [r for r in runs if item_for.get(r['id']) == selected_item]
    chosen = next((r for r in available if r['id'] == run_id), None) if run_id else next(iter(available), None)
    if run_id and chosen is None:
        raise HTTPException(422, 'Run is not part of this item')
    info = {'items': [{'id': n['id'], 'label': n['label']} for n in items],
            'runs': [{'id': n['id'], 'run_uuid': n['properties'].get('run_uuid'),
                      'completed_at': n['properties'].get('completed_at')} for n in available],
            'item_id': selected_item, 'run_id': chosen['id'] if chosen else '',
            'chronology_known': bool(chosen and chosen['properties'].get('completed_at'))}
    if not chosen:
        return {**info, 'nodes': [], 'edges': [], 'path_ids': []}
    ids = {chosen['id'], selected_item}
    # Follow only run-owned outputs and explicitly recorded supporting entities.
    follow = {'USES_EVIDENCE', 'HAS_IMAGE_REFERENCE', 'CONSIDERS', 'SEARCHED', 'HAS_INVOCATION',
              'RESULTED_IN', 'SKIPPED', 'PRODUCED', 'USES_MODEL', 'BELONGS_TO', 'OBSERVED_GPU',
              'LOCATED_ON', 'REFERENCES', 'RETURNED', 'EVALUATED', 'EXECUTED_BY', 'RUNS_ON',
              'ACCEPTED_AS', 'UPDATED', 'PERSISTED_AS'}
    queue = [chosen['id']]
    while queue:
        source = queue.pop()
        for e in edges:
            if e['source'] == source and e['kind'] in follow and e['target'] not in ids:
                ids.add(e['target'])
                # OCR passes shared across runs must not pull in their other outputs.
                if nodes[e['target']]['kind'] != 'Observation':
                    queue.append(e['target'])
    for e in edges:
        if e['kind'] == 'PRODUCED' and e['target'] in ids and nodes[e['source']]['kind'] == 'OCRPass':
            ids.add(e['source'])
    scoped = [e for e in edges if e['source'] in ids and e['target'] in ids]
    # A selected evidence path is explicit provenance, not an endorsement of model assertions.
    path = {n for n in ids if nodes[n]['kind'] == 'Decision'}
    path.update(e['target'] for e in scoped if e['kind'] in {'RECOMMENDS', 'ACCEPTED_AS'})
    provenance = {'SUPPORTS', 'CITES', 'SELECTS', 'INFORMS', 'PRODUCED', 'RETURNED',
                  'PROPOSED_SEARCH', 'DERIVED_FROM'}
    changed = True
    while changed:
        before = len(path)
        for e in scoped:
            if e['kind'] in provenance and e['target'] in path:
                path.add(e['source'])
            if e['kind'] in {'CITES', 'DERIVED_FROM'} and e['source'] in path:
                path.add(e['target'])
        changed = len(path) != before
    return {**info, 'nodes': [nodes[n] for n in nodes if n in ids], 'edges': scoped,
            'path_ids': sorted(path)}


@app.get(f'{BASE_PATH}/api/graph/receipts/{{sha}}')
def receipt(sha: str, perspective: str = 'lineage', offset: int = Query(0, ge=0),
            limit: int = Query(100, ge=1, le=200), item_id: str = '', run_id: str = '',
            _: dict = Depends(authenticated_identity)):
    if perspective not in PERSPECTIVES or not graph.re.fullmatch('[0-9a-f]{64}', sha):
        raise HTTPException(422, 'Invalid receipt or perspective')
    rows = read('MATCH (s:ReceiptSnapshot {sha:$sha}) RETURN s.graph_json AS graph,s.observed_at AS observed_at', sha=sha)
    if not rows:
        raise HTTPException(404, 'Receipt not projected yet')
    result = json.loads(rows[0]['graph'])
    if perspective == 'story':
        story = processing_story(result, item_id, run_id)
        for node in story['nodes']:
            node['links'] = links(node)
        return {**story, 'total_nodes': len(story['nodes']), 'offset': 0,
                'observed_at': rows[0]['observed_at']}
    kinds = PERSPECTIVES[perspective]
    nodes = [n for n in result['nodes'] if kinds is None or n['kind'] in kinds]
    # Breadth-first order keeps connected steps visible on the first page.
    adjacency = {}
    eligible = {n['id']: n for n in nodes}
    for edge in result['edges']:
        if edge['source'] in eligible and edge['target'] in eligible:
            adjacency.setdefault(edge['source'], []).append(edge['target'])
            adjacency.setdefault(edge['target'], []).append(edge['source'])
    queue = [n['id'] for n in nodes if n['kind'] == 'Receipt']
    ordered, seen = [], set()
    while queue:
        key = queue.pop(0)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(eligible[key])
        queue.extend(adjacency.get(key, []))
    ordered.extend(n for n in nodes if n['id'] not in seen)
    selected = ordered[offset:offset + limit]
    ids = {n['id'] for n in selected}
    for node in selected:
        node['links'] = links(node)
    return {'nodes': selected, 'edges': [e for e in result['edges'] if e['source'] in ids and e['target'] in ids],
            'total_nodes': len(nodes), 'offset': offset, 'limit': limit, 'observed_at': rows[0]['observed_at']}


@app.get(f'{BASE_PATH}/api/graph/telemetry')
def telemetry(_: dict = Depends(authenticated_identity)):
    """Fixed PromQL only; the browser cannot select upstreams or arbitrary queries."""
    base = os.getenv('GRAPH_PROMETHEUS_URL')
    now = datetime.now(timezone.utc).isoformat()
    try:
        workers = kubernetes_health()
    except Exception:
        workers = []
    common = {'observed_at': now, 'workers': workers, 'refresh_seconds': 30, 'stale_seconds': 90,
              'worker_source': 'Kubernetes Pod Ready condition; absent historical pods have unknown current health'}
    if not base:
        return {**common, 'status': 'available' if workers else 'unavailable', 'samples': [], 'metrics_status': 'unavailable'}
    try:
        url = base.rstrip('/') + '/api/v1/query?' + urlencode({'query': 'up'})
        with urllib.request.urlopen(url, timeout=3) as response:
            result = json.load(response)
        if result.get('status') != 'success':
            raise ValueError('Prometheus unavailable')
        return {**common, 'status': 'available', 'metrics_status': 'available', 'samples': result['data']['result'],
                'refresh_seconds': 30, 'stale_seconds': 90, 'source': 'Prometheus scrape health; historical workers without a current target remain unknown'}
    except Exception:
        return {**common, 'status': 'available' if workers else 'unavailable', 'samples': [], 'metrics_status': 'unavailable'}


@app.get(f'{BASE_PATH}/graph', response_class=HTMLResponse)
def canvas(identity: dict = Depends(authenticated_identity)):
    body = Path(__file__).with_name('receipt_graph.html').read_text().replace('__BASE_PATH__', json.dumps(BASE_PATH).replace('<', '\\u003c'))
    return page('Receipt knowledge graph', body, base_path=BASE_PATH, identity=identity)
