import uuid

import pytest
from psycopg.types.json import Jsonb

from test_receipt_recommendation_acceptance_db import receipt, accept
from home_budget_pipeline import receipt_graph

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def taxonomy(receipt):
    service = receipt[0]
    with service._connect() as conn:
        group = conn.execute("INSERT INTO budget.category_groups(group_key,group_name) VALUES ('refresh-tests','Refresh tests') ON CONFLICT(group_key) DO UPDATE SET group_name=EXCLUDED.group_name RETURNING id").fetchone()['id']
        for name in ['Groceries', 'Indoor Supplies', 'Uncategorized']:
            conn.execute('INSERT INTO budget.expense_categories(category_name,group_id) VALUES (%s,%s) ON CONFLICT(category_name) DO UPDATE SET is_active=TRUE', (name, group))
        conn.execute('DELETE FROM enrichment.product_cache')
    yield


def refresh(receipt):
    return receipt[0].refresh_receipt_items(receipt[1], actor_user='paul', actor_email='paul@example.com')


def test_acceptance_categorizes_salsa_and_refresh_repairs_duplicate_and_existing_acceptance(receipt):
    service,sha,expense,items,_,_ = receipt
    accept(receipt)
    assert service._fetch('SELECT budget_category FROM budget.expense_items WHERE id=%s', (items[0],))[0]['budget_category'] == 'Groceries'
    with service._connect() as conn:
        # Existing accepted rows can predate category-on-acceptance support.
        conn.execute("UPDATE budget.expense_items SET budget_category='Uncategorized' WHERE id=%s", (items[0],))
        conn.execute("INSERT INTO budget.expense_items(expense_pk,item_name,line_total) VALUES (%s,'Chips Multigrain',5.29)", (expense,))
    result = refresh(receipt)
    assert result == {'products_applied': 1, 'categories_applied': 3, 'products_pending': 1}
    rows = service._fetch('SELECT * FROM budget.expense_items WHERE expense_pk=%s ORDER BY id', (expense,))
    assert all(row['budget_category'] == 'Groceries' for row in rows)
    assert rows[1]['product_description'] == rows[0]['product_description']
    assert str(rows[1]['line_total']) == '6.49' and rows[1]['item_name'] == 'Cep Pic Med'
    assert rows[2]['product_description'] is None
    enrichment = service._fetch('SELECT * FROM budget.product_enrichment_results WHERE expense_item_id=%s', (items[1],))[0]
    assert enrichment['provider'] == 'accepted-cache' and enrichment['status'] == 'accepted'
    assert len(service._fetch('SELECT * FROM budget.expense_item_description_audit WHERE expense_item_id=%s', (items[1],))) == 1
    assert refresh(receipt) == {'products_applied': 0, 'categories_applied': 0, 'products_pending': 1}
    with service._connect() as conn:
        graph = receipt_graph.build(receipt_graph.load(conn, sha))
    assert any(n['kind'] == 'ItemRefresh' for n in graph['nodes'])


@pytest.mark.parametrize('protection', ['manual_category', 'manual_description', 'review', 'weak_cache', 'recipe', 'wrong_merchant'])
def test_refresh_preserves_manual_fields_and_rechecks_cache_evidence(receipt, protection):
    service,sha,expense,items,_,p = receipt
    accept(receipt)
    with service._connect() as conn:
        if protection == 'manual_category':
            conn.execute("UPDATE budget.expense_items SET budget_category='Indoor Supplies',category_rationale='human-approved category override' WHERE id=%s", (items[1],))
        elif protection == 'manual_description':
            conn.execute("UPDATE budget.expense_items SET product_description='Manual text' WHERE id=%s", (items[1],))
        elif protection == 'review':
            p['decision']['disposition'] = 'review'
            conn.execute('INSERT INTO enrichment.receipt_collaborations(run_uuid,expense_item_id,payload) VALUES (%s,%s,%s)', (uuid.uuid4(), items[1], Jsonb(p)))
        elif protection == 'weak_cache':
            conn.execute('UPDATE enrichment.product_cache SET confidence=.2')
        elif protection == 'recipe':
            conn.execute("UPDATE enrichment.product_cache SET product_url='https://www.sobeys.com/recipes/salsa'")
        elif protection == 'wrong_merchant':
            conn.execute("UPDATE enrichment.product_cache SET merchant_key='costco.ca'")
    result = refresh(receipt)
    item = service._fetch('SELECT * FROM budget.expense_items WHERE id=%s', (items[1],))[0]
    if protection == 'manual_category':
        assert item['budget_category'] == 'Indoor Supplies'
    else:
        assert result['products_applied'] == 0
        assert item['product_description'] == ('Manual text' if protection == 'manual_description' else None)


def test_receipt_refresh_rolls_back_products_and_categories_if_lineage_audit_fails(receipt):
    service,sha,expense,items,_,_ = receipt
    accept(receipt)
    with service._connect() as conn:
        conn.execute("CREATE FUNCTION lineage.reject_refresh_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.payload->>'event'='receipt_item_refreshed' THEN RAISE EXCEPTION 'audit failure'; END IF; RETURN NEW; END $$")
        conn.execute('CREATE TRIGGER reject_refresh_test BEFORE INSERT ON lineage.receipt_events FOR EACH ROW EXECUTE FUNCTION lineage.reject_refresh_test()')
    try:
        with pytest.raises(Exception, match='audit failure'):
            refresh(receipt)
        item = service._fetch('SELECT * FROM budget.expense_items WHERE id=%s', (items[1],))[0]
        assert item['product_description'] is None and item['budget_category'] is None
        assert not service._fetch('SELECT * FROM budget.expense_item_description_audit WHERE expense_item_id=%s', (items[1],))
    finally:
        with service._connect() as conn:
            conn.execute('DROP TRIGGER reject_refresh_test ON lineage.receipt_events')
            conn.execute('DROP FUNCTION lineage.reject_refresh_test()')


def test_acceptance_prefers_approved_scoped_rule_and_preserves_manual_category(receipt):
    service,_,_,items,_,_ = receipt
    with service._connect() as conn:
        rule = conn.execute('''INSERT INTO budget.expense_category_mappings
            (source,merchant,match_type,match_text,category_name,is_approved)
            VALUES ('sobeys','Sobeys','exact','cep pic med','Indoor Supplies',TRUE) RETURNING id''').fetchone()['id']
    try:
        accept(receipt)
        row = service._fetch('SELECT budget_category,category_rationale FROM budget.expense_items WHERE id=%s', (items[0],))[0]
        assert row['budget_category'] == 'Indoor Supplies'
        assert row['category_rationale'] == 'approved category rule applied during product review'
    finally:
        with service._connect() as conn:
            conn.execute('DELETE FROM budget.expense_category_mappings WHERE id=%s', (rule,))


def test_unknown_receipt_cannot_refresh_other_receipts(receipt):
    with pytest.raises(LookupError):
        receipt[0].refresh_receipt_items('f'*64, actor_user='paul')
    assert receipt[0]._fetch('SELECT product_description FROM budget.expense_items WHERE id=%s', (receipt[3][1],))[0]['product_description'] is None
