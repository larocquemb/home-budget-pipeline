import json
import os
import uuid

import pytest

from home_budget_pipeline.receipt_graph import Graph, write

pytestmark = pytest.mark.integration


@pytest.fixture
def pg_conn():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], row_factory=dict_row) as conn:
        yield conn
        conn.rollback()


def test_real_postgres_attempt_history_is_authoritative(pg_conn):
    from psycopg.types.json import Jsonb
    event = uuid.uuid4()
    sha = uuid.uuid4().hex * 2
    try:
        pg_conn.execute('INSERT INTO ingest.receipts(source_sha256,source_reference) VALUES (%s,%s)', (sha,'synthetic-receipt.pdf'))
        pg_conn.execute('INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)',
                        (event, sha, 'synthetic-receipt.pdf', Jsonb({'status': 'failed', 'attempt_id': str(event),
                            'message_id': 'message:'+sha, 'operation': 'receipt.cache-rebuild.v3', 'attempt': 1})))
        row = pg_conn.execute('SELECT payload FROM lineage.receipt_events WHERE id=%s', (event,)).fetchone()
        payload = row['payload'] if isinstance(row, dict) else row[0]
        assert payload['status'] == 'failed'
        from home_budget_pipeline.receipt_graph import load, build
        graph = build(load(pg_conn, sha))
        assert any(n['kind'] == 'AttemptEvent' and n['properties']['status'] == 'failed' for n in graph['nodes'])
    finally:
        pg_conn.rollback()


def test_real_neo4j_atomic_replay_and_out_of_order_snapshot(monkeypatch):
    if not os.getenv('TEST_NEO4J_URI'):
        pytest.skip('TEST_NEO4J_URI not configured')
    from neo4j import GraphDatabase
    sha = uuid.uuid4().hex * 2
    graph = Graph()
    a = graph.node('Receipt', sha, 'Synthetic receipt')
    b = graph.node('Model', sha, 'Synthetic model')
    graph.edge(a, 'USES_MODEL', b)
    data = graph.export()
    with GraphDatabase.driver(os.environ['TEST_NEO4J_URI'], auth=('neo4j', 'test-graph-password')) as driver, driver.session(database='neo4j') as session:
        try:
            session.run('CREATE CONSTRAINT receipt_flow_id IF NOT EXISTS FOR (n:ReceiptFlowEntity) REQUIRE n.id IS UNIQUE').consume()
            session.run('CREATE CONSTRAINT receipt_snapshot_sha IF NOT EXISTS FOR (n:ReceiptSnapshot) REQUIRE n.sha IS UNIQUE').consume()
            for _ in range(2):
                assert session.execute_write(write, sha, data, '2026-10-04T15:00:00+00:00')
            assert session.run('MATCH (n:ReceiptFlowEntity) WHERE n.id IN $ids RETURN count(n) AS c', ids=[a, b]).single()['c'] == 2
            assert session.run('MATCH (a:ReceiptFlowEntity {id:$id})-[r]->() RETURN count(r) AS c', id=a).single()['c'] == 1
            assert not session.execute_write(write, sha, {'nodes': [], 'edges': []}, '2026-10-03T15:00:00+00:00')
            malformed = {**data, 'edges': [{**data['edges'][0], 'kind': 'INVALID; QUERY'}]}
            with pytest.raises(ValueError):
                session.execute_write(write, sha, malformed, '2026-10-05T15:00:00+00:00')
            row = session.run('MATCH (s:ReceiptSnapshot {sha:$sha}) RETURN s.graph_json AS graph,s.observed_at AS time', sha=sha).single()
            assert json.loads(row['graph']) == data
            assert row['time'] == '2026-10-04T15:00:00+00:00'
            from home_budget_pipeline.web import receipt_graph as web
            def read(query, **params):
                assert 's.graph_json' not in query
                return session.execute_read(lambda tx: [r.data() for r in tx.run(query, **params)])
            monkeypatch.setattr(web, 'read', read)
            index, time = web.snapshot_index(sha)
            assert index['edges'] == data['edges']
            assert {n['id'] for n in index['nodes']} == {a, b}
            assert web.snapshot_nodes(sha, time, [b, a]) == [
                next(n for n in data['nodes'] if n['id'] == key) for key in (b, a)]
            result = web.receipt(sha, perspective='lineage', offset=0, limit=1)
            assert len(result['nodes']) == 1
            assert result['nodes'][0]['id'] == a

        finally:
            session.run('MATCH (n:ReceiptFlowEntity) WHERE n.id IN $ids DETACH DELETE n', ids=[a,b]).consume()
            session.run('MATCH (s:ReceiptSnapshot {sha:$sha}) DETACH DELETE s', sha=sha).consume()


def test_postgres_graph_load_scopes_queued_tasks_and_omits_task_inputs(pg_conn):
    from psycopg.types.json import Jsonb
    from uuid import uuid5
    from home_budget_pipeline.receipt_graph import load, build
    sha, run_id = uuid.uuid4().hex * 2, uuid.uuid4()
    pg_conn.execute('INSERT INTO ingest.receipts(source_sha256,source_reference) VALUES (%s,%s)', (sha, 'queue-provenance.pdf'))
    expense = pg_conn.execute("INSERT INTO budget.expenses(source,order_id,store_name,receipt_filename,expense_total) VALUES ('scanned',%s,'Sobeys','queue-provenance.pdf',1) RETURNING id", (str(run_id),)).fetchone()['id']
    item = pg_conn.execute("INSERT INTO budget.expense_items(expense_pk,item_name) VALUES (%s,'Item') RETURNING id", (expense,)).fetchone()['id']
    pg_conn.execute("INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,source_sha256,is_primary_source) VALUES (%s,'scanned','queue-provenance.pdf',%s,true)", (expense, sha))
    payload = {'run_uuid': str(run_id), 'item_id': item, 'independent_results': [], 'reviews': [], 'decision': {'disposition': 'review'}}
    pg_conn.execute('INSERT INTO enrichment.receipt_collaborations(run_uuid,expense_item_id,payload) VALUES (%s,%s,%s)', (run_id, item, Jsonb(payload)))
    for current in (True, False):
        cohort = uuid5(run_id, f'{item if current else item+1}:independent')
        pg_conn.execute('''INSERT INTO enrichment.model_collaboration_tasks(id,cohort,provider,input,deadline)
            VALUES (%s,%s,'qwen',%s,NOW()+interval '1 hour')''', (uuid.uuid4(), cohort, Jsonb({
            'stage': 'independent', 'profile': {'name': 'qwen'}, 'reply': 'receipt-models.v1.results.' + str(cohort),
            'unused_private_input': 'DO-NOT-PROJECT'})))
    data = load(pg_conn, sha)
    data['collaborations'] = list(data['collaborations'])
    assert len(data['collaborations'][0]['queued_tasks']) == 1
    graph = build(data)
    assert len([n for n in graph['nodes'] if n['kind'] == 'ModelTask']) == 1
    assert 'DO-NOT-PROJECT' not in json.dumps(graph)
