import os
import uuid
from pathlib import Path
from decimal import Decimal

import pytest
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from home_budget_pipeline.web.queries import LedgerQueryService, RecommendationConflict
from home_budget_pipeline.web.receipt_app import _receipt_detail
from home_budget_pipeline import receipt_graph
from home_budget_pipeline.product_labels import product_name
from home_budget_pipeline.web.receipt_graph import processing_story
from test_receipt_recommendation_acceptance import payload

pytestmark = pytest.mark.integration


@pytest.fixture
def receipt():
    dsn = os.environ['TEST_DATABASE_URL']
    source = uuid.uuid4().hex*2
    run = str(uuid.uuid4())
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute(Path('sql/analytics_views.sql').read_text())
        conn.execute('INSERT INTO ingest.receipts(source_sha256,source_reference) VALUES (%s,%s)', (source,'acceptance.pdf'))
        expense = conn.execute("INSERT INTO budget.expenses(source,order_id,store_name,expense_total) VALUES ('sobeys',%s,'Sobeys',12.98) RETURNING id", (source,)).fetchone()['id']
        conn.execute("INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_sha256,source_reference) VALUES (%s,'scanned',%s,'acceptance.pdf')", (expense,source))
        items = [conn.execute("INSERT INTO budget.expense_items(expense_pk,item_name,unit_qty,unit_cost,line_total) VALUES (%s,'Cep Pic Med',1,6.49,6.49) RETURNING id",(expense,)).fetchone()['id'] for _ in range(2)]
        p = payload();p.update(run_uuid=run,item_id=items[0])
        raw='Buy '+p['decision']['candidate_title']+' | Sobeys Inc.'
        p['decision']['candidate_title']=p['search_results'][0]['title']=raw
        collaboration = conn.execute('INSERT INTO enrichment.receipt_collaborations(run_uuid,expense_item_id,payload) VALUES (%s,%s,%s) RETURNING id', (run,items[0],Jsonb(p))).fetchone()['id']
    service = LedgerQueryService(connect=lambda: psycopg.connect(dsn,row_factory=dict_row))
    yield service,source,expense,items,collaboration,p
    with psycopg.connect(dsn) as conn:
        conn.execute('DELETE FROM lineage.receipt_events WHERE source_sha256=%s',(source,))
        conn.execute('DELETE FROM budget.expenses WHERE id=%s',(expense,))
        conn.execute('DELETE FROM ingest.receipts WHERE source_sha256=%s',(source,))


def accept(receipt, **overrides):
    service,source,_,items,collaboration,_ = receipt
    return service.accept_product_recommendation(overrides.pop('source',source),items[0],
        overrides.pop('collaboration',collaboration),expected_description=None,expected_url=None,
        actor_user='paul',actor_email='paul@example.com',**overrides)


def test_acceptance_updates_one_duplicate_item_and_retains_audit_and_graph_provenance(receipt):
    service,source,expense,items,collaboration,p = receipt
    first = accept(receipt)
    repeat = accept(receipt)
    assert repeat['already_accepted'] and repeat['audit_id'] == first['audit_id']
    rows = service._fetch('SELECT item_name,line_total,product_description,product_url FROM budget.expense_items WHERE expense_pk=%s ORDER BY id',(expense,))
    assert all(r['item_name']=='Cep Pic Med' and r['line_total']==Decimal('6.49') for r in rows)
    assert rows[0]['product_description']==product_name(p['decision']['candidate_title'],p['decision']['candidate_url'])
    assert service._fetch('SELECT candidate_title FROM budget.product_enrichment_results WHERE expense_item_id=%s',(items[0],))[0]['candidate_title']==p['decision']['candidate_title']
    assert rows[1]['product_description'] is None and rows[1]['product_url'] is None
    audits = service._fetch('SELECT * FROM budget.expense_item_description_audit WHERE expense_item_id=%s',(items[0],))
    assert len(audits)==1 and audits[0]['actor_user']=='paul' and audits[0]['old_description'] is None
    detail = _receipt_detail(service,source)
    assert detail['expense']['items'][0]['recommendation']['accepted_at'] is not None
    assert detail['expense']['items'][0]['enrichment']['status']=='accepted'
    assert detail['expense']['items'][1]['enrichment'] is None
    assert service._fetch("SELECT status FROM enrichment.product_cache WHERE merchant_key='sobeys.com' AND receipt_text_norm='cep pic med'")[0]['status']=='accepted'
    with service._connect() as conn:
        graph = receipt_graph.build(receipt_graph.load(conn,source))
    story = processing_story(graph)
    assert len([n for n in story['nodes'] if n['kind']=='ProductAcceptance'])==1
    assert {'ACCEPTED_AS','UPDATED','PERSISTED_AS'} <= {e['kind'] for e in story['edges']}
    assert next(n for n in graph['nodes'] if n['kind']=='Decision')['properties']['canonical_updated'] is False


@pytest.mark.parametrize('conflict',['receipt','newer_run','changed_item','blocked','rollback'])
def test_acceptance_rejects_stale_or_ineligible_requests_and_rolls_back(receipt,conflict):
    service,source,expense,items,collaboration,p = receipt
    if conflict=='receipt':
        with pytest.raises(LookupError): accept(receipt,source='f'*64)
    else:
        with service._connect() as conn:
            if conflict=='newer_run':
                conn.execute('INSERT INTO enrichment.receipt_collaborations(run_uuid,expense_item_id,payload) VALUES (%s,%s,%s)',(uuid.uuid4(),items[0],Jsonb(p)))
            if conflict=='changed_item':
                conn.execute("UPDATE budget.expense_items SET product_description='Manual correction' WHERE id=%s",(items[0],))
            if conflict=='blocked':
                p['decision']['disposition']='review'
                conn.execute('UPDATE enrichment.receipt_collaborations SET payload=%s WHERE id=%s',(Jsonb(p),collaboration))
        if conflict=='rollback':
            # Force the audit insert to fail after the canonical update.
            original_connect=service._connect
            # A DB trigger provides a deterministic late transaction failure.
            with original_connect() as conn:
                conn.execute("CREATE FUNCTION budget.reject_acceptance_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit failure'; END $$")
                conn.execute('CREATE TRIGGER reject_acceptance_test BEFORE INSERT ON budget.expense_item_description_audit FOR EACH ROW EXECUTE FUNCTION budget.reject_acceptance_test()')
            try:
                with pytest.raises(psycopg.errors.RaiseException): accept(receipt)
            finally:
                with original_connect() as conn:
                    conn.execute('DROP TRIGGER reject_acceptance_test ON budget.expense_item_description_audit')
                    conn.execute('DROP FUNCTION budget.reject_acceptance_test()')
        else:
            with pytest.raises((RecommendationConflict,ValueError)): accept(receipt)
    rows=service._fetch('SELECT product_description FROM budget.expense_items WHERE id=%s',(items[0],))
    assert rows[0]['product_description']==('Manual correction' if conflict=='changed_item' else None)
    assert not service._fetch('SELECT id FROM budget.expense_item_description_audit WHERE expense_item_id=%s',(items[0],))
    assert not service._fetch('SELECT id FROM budget.product_enrichment_results WHERE expense_item_id=%s',(items[0],))


def test_old_acceptances_with_raw_webpage_titles_remain_idempotent(receipt):
    service,source,expense,items,collaboration,p=receipt
    first=accept(receipt)
    with service._connect() as conn:
        conn.execute('UPDATE budget.expense_items SET product_description=%s WHERE id=%s', (p['decision']['candidate_title'],items[0]))
        conn.execute("UPDATE lineage.receipt_events SET payload=jsonb_set(payload,'{product_description}',%s) WHERE source_sha256=%s", (Jsonb(p['decision']['candidate_title']),source))
    repeat=accept(receipt)
    assert repeat['already_accepted'] and repeat['audit_id']==first['audit_id']
    assert repeat['product_description']==p['decision']['candidate_title']
    assert len(service._fetch('SELECT id FROM budget.expense_item_description_audit WHERE expense_item_id=%s',(items[0],)))==1
