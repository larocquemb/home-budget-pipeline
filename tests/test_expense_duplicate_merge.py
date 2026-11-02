"""Already-booked duplicate consolidation preserves source data and audit history."""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from home_budget_pipeline.receipts import expense_merge
from home_budget_pipeline.receipts.duplicate_review import version


@pytest.fixture
def state():
    return {'expenses':[{'id':11,'expense_total':'24.60','raw_payload':{}},
                        {'id':22,'expense_total':'24.68','raw_payload':{}}],
            'items':[{'id':101,'expense_pk':11,'item_name':'Verified item','budget_category':'Groceries'},
                     {'id':201,'expense_pk':22,'item_name':'OCR item','budget_category':None}],
            'evidence':[{'id':31,'expense_pk':11,'source_sha256':'a'*64,'source_reference':'a.pdf','evidence_type':'scanned'},
                        {'id':32,'expense_pk':22,'source_sha256':'b'*64,'source_reference':'b.png','evidence_type':'scanned'}],
            'decisions':[], 'transactions':[], 'enrichment':[]}


@pytest.fixture
def db(monkeypatch,state):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [{'viewname':name,'definition':'WHERE NOT raw_payload ? duplicate_merge'}
        for name in ('analytics_expenses','analytics_expense_items','analytics_category_spend')]
    monkeypatch.setattr(expense_merge,'load',lambda *a,**kw: deepcopy(state))
    from home_budget_pipeline.receipts import deduplication_store
    monkeypatch.setattr(deduplication_store,'PostgresReceiptDuplicateStore',lambda *a,**kw: MagicMock())
    return conn,cur


def call(db,**kw):
    return expense_merge.merge(lambda:db[0],11,22,reason='Reviewer identified same purchase',actor='Test reviewer',**kw)


def token(state):
    return version({'keeper':11,'duplicate':22,'state':state})


def test_preview_is_read_only_and_exposes_both_original_item_sets(db,state):
    result = call(db)
    assert result['version'] == token(state)
    assert result['before']['items'] == state['items']
    db[1].execute.assert_called_once_with('SET TRANSACTION READ ONLY')
    db[0].commit.assert_not_called()


def test_apply_relinks_sources_and_soft_supersedes_only_duplicate(db,state):
    result = call(db,apply=True,expected_version=token(state))
    assert result['expense_pk'] == 11 and result['superseded_expense_pk'] == 22
    statements = [(args[0],args[1] if len(args)>1 else ()) for args,_ in db[1].execute.call_args_list]
    assert not any('DELETE' in sql for sql,_ in statements)
    assert not any('UPDATE budget.expense_items' in sql for sql,_ in statements)
    expense_update, = [(sql,params) for sql,params in statements if 'UPDATE budget.expenses ' in sql]
    assert expense_update[1][-1] == 22
    evidence_update, = [(sql,params) for sql,params in statements if 'UPDATE budget.receipt_evidence' in sql]
    assert evidence_update[1][0] == 11 and evidence_update[1][-1] == 22
    audit, = [params[-1].obj for sql,params in statements if 'INSERT INTO lineage.receipt_events' in sql]
    assert audit['before'] == state and audit['event'] == 'receipt_expenses_merged'
    db[0].commit.assert_called_once()


@pytest.mark.parametrize('field,value', [('transactions',[{'transaction_id':1,'expense_pk':22}]),
    ('enrichment',[{'expense_pk':22}]),('decisions',[{'resolution_status':'rejected'}])])
def test_conflicting_financial_or_duplicate_decisions_are_not_silently_rewritten(db,state,field,value):
    state[field] = value
    with pytest.raises(ValueError): call(db,apply=True,expected_version=token(state))
    assert not any('UPDATE' in args[0] for args,_ in db[1].execute.call_args_list)
    db[0].commit.assert_not_called()


def test_stale_preview_and_old_reporting_views_cannot_apply(db,state):
    with pytest.raises(ValueError,match='changed'):call(db,apply=True,expected_version='a'*64)
    db[1].fetchall.return_value = []
    with pytest.raises(ValueError,match='database setup'):call(db,apply=True,expected_version=token(state))
    assert not any('UPDATE' in args[0] for args,_ in db[1].execute.call_args_list)


def test_audit_failure_rolls_back_whole_merge(db,state):
    def execute(sql,*a):
        if 'INSERT INTO lineage.receipt_events' in sql: raise RuntimeError('audit failed')
    db[1].execute.side_effect = execute
    with pytest.raises(RuntimeError,match='audit failed'):call(db,apply=True,expected_version=token(state))
    db[0].rollback.assert_called_once()
    db[0].commit.assert_not_called()


def test_retry_of_same_merge_does_not_create_another_audit(db,state):
    state['expenses'][1]['raw_payload'] = {'duplicate_merge':{'canonical_expense_pk':11,'audit_id':'prior'}}
    state['evidence'][1]['expense_pk'] = 11
    assert call(db,apply=True,expected_version='old')['unchanged']
    assert not any('UPDATE' in args[0] for args,_ in db[1].execute.call_args_list)


def test_all_active_reporting_views_exclude_superseded_purchases():
    from home_budget_pipeline import db_setup
    sql = db_setup.ANALYTICS_SCHEMA.read_text()
    for name in ('analytics_expenses','analytics_expense_items','analytics_category_spend'):
        definition = sql.split('VIEW budget.'+name+' AS',1)[1].split(';',1)[0]
        assert "WHERE NOT (COALESCE(e.raw_payload, '{}'::jsonb) ? 'duplicate_merge')" in definition


def test_code_selects_validated_source_over_higher_confidence_bad_amount(state):
    state['expenses'][0].update(receipt_item_subtotal='24.60',extraction_confidence=.60)
    state['expenses'][1].update(receipt_item_subtotal='20.00',extraction_confidence=.99)
    selected = expense_merge.select_source(state)
    assert selected['expense_pk'] == 11
    state['expenses'][1]['receipt_item_subtotal'] = '24.68'
    assert expense_merge.select_source(state)['expense_pk'] == 22
    state['expenses'][1]['raw_payload'] = {}
    state['evidence'][1]['raw_payload'] = {'field_decisions':{'transaction_datetime':{'status':'conflict'}}}
    assert expense_merge.select_source(state)['expense_pk'] == 11


def test_auto_selection_is_argument_order_independent_and_audited(db,state):
    state['expenses'][0].update(receipt_item_subtotal='15.00',extraction_confidence=.99)
    state['expenses'][1].update(receipt_item_subtotal='24.68',extraction_confidence=.70)
    preview = call(db,auto_select=True)
    assert preview['keeper'] == 22 and preview['duplicate'] == 11
    result = call(db,auto_select=True,apply=True,expected_version=preview['version'])
    assert result['expense_pk'] == 22
    audits = [args[1][-1].obj for args,_ in db[1].execute.call_args_list if 'INSERT INTO lineage.receipt_events' in args[0]]
    assert audits[0]['selection']['policy'] == 'validated-source-quality-v2'


def test_inferred_balancing_tax_cannot_make_a_bad_total_pass(state):
    state['expenses'][0].update(receipt_item_subtotal='20.00',expense_total='25.00',receipt_gst='5.00',
        raw_payload={'tax_components':{'gst':1.00},'review_reasons':['item_total_mismatch']})
    state['expenses'][1].update(receipt_item_subtotal='24.00',expense_total='25.00',receipt_gst='1.00',
        raw_payload={'tax_components':{'gst':1.00},'review_reasons':['item_total_mismatch']})
    state['items'][0]['line_total'] = '27.00'
    state['items'][1]['line_total'] = '29.00'
    selection = expense_merge.select_source(state)
    assert selection['expense_pk'] == 22 and selection['requires_review']
    left,right = selection['comparison']
    assert left['printed_tax_agrees'] is False and left['monetary_reconciliation'] == -1
    assert right['printed_tax_agrees'] is True and right['monetary_reconciliation'] == 1
    assert left['item_reconciliation'] == right['item_reconciliation'] == -1


def test_item_reconciliation_precedes_coverage_confidence(state):
    for row in state['expenses']:
        row.update(receipt_item_subtotal='10.00',expense_total='10.00')
    state['items'][0]['line_total'] = '10.00'
    state['items'][1]['line_total'] = '99.00'
    state['evidence'][0]['extraction_confidence'] = .80
    state['evidence'][1]['extraction_confidence'] = .99
    selection = expense_merge.select_source(state)
    assert selection['expense_pk'] == 11
    assert selection['comparison'][1]['item_subtotal_variance'] == '89.00'
