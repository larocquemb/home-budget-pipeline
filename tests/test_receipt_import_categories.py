"""Shared import/category decisions and preservation across repeated OCR refreshes."""
from decimal import Decimal
from types import SimpleNamespace

from home_budget_pipeline.categorization.item_rules import category_for_product, apply_category
from home_budget_pipeline.categorization import receipt_items
from home_budget_pipeline.receipts import ingest


def test_section_supports_existing_rules_without_becoming_the_category():
    for section in ('MEAT', 'BAKERY'):
        result = category_for_product({'item_name':'Unidentified product','department':section}, '', [], {'Groceries'})
        assert result.category == 'Groceries'
        assert 'printed section supports' in result.rationale
    assert category_for_product({'item_name':'Unidentified product','department':'PARTS'}, '', [], {'Groceries'}) is None
    assert category_for_product({'item_name':'Unidentified product','department':'MEAT'}, '', [], set()) is None
    result = category_for_product({'item_name':'Dish soap','department':'MEAT'}, '', [], {'Indoor Supplies','Groceries'})
    assert result.category == 'Indoor Supplies'


def test_approved_item_rule_and_manual_decisions_precede_section():
    item = {'item_name':'Unidentified product','department':'BAKERY','source':'scanned','store_name':'Example Shop'}
    rule = {'source':'scanned','merchant':'Example Shop','match_type':'exact',
            'match_text':item['item_name'],'category_name':'Indoor Supplies'}
    assert category_for_product(item,'',[rule],{'Groceries','Indoor Supplies'}).category == 'Indoor Supplies'
    assert category_for_product({**item,'category_source':'manual'},'',[rule],{'Groceries'}) is None
    assert category_for_product({**item,'budget_category':'Indoor Supplies'},'',[rule],{'Groceries','Indoor Supplies'}) is None


class Cursor:
    def __init__(self, dict_rows=False):
        self.calls = []
        self.sql = ''
        self.next_id = 100
        self.dict_rows = dict_rows
    def execute(self, sql, params=()):
        self.sql = sql
        self.calls.append((sql, params))
    def fetchone(self):
        self.next_id += 1
        return {'id':self.next_id} if self.dict_rows else (self.next_id,)
    def fetchall(self):
        if 'SELECT category_name' in self.sql:
            return [{'category_name':'Groceries'}] if self.dict_rows else [('Groceries',)]
        return []


def test_import_and_web_share_category_rules_with_both_cursor_row_formats():
    from home_budget_pipeline.web.product_categories import apply_category as web_apply
    assert web_apply is apply_category
    for dict_rows in (False, True):
        cur = Cursor(dict_rows)
        result = apply_category(cur, {'id':1,'item_name':'Bread'}, '', schema='example_budget')
        assert result['new_category'] == 'Groceries'
        assert any('UPDATE example_budget.expense_items' in sql for sql,_ in cur.calls)
        assert not any('budget.expense_items' in sql.replace('example_budget.expense_items','') for sql,_ in cur.calls)


def test_repeated_refresh_keeps_ids_categories_descriptions_and_duplicate_counts():
    cur = Cursor()
    existing = [{'id':1,'item_name':'Mystery product','line_total':Decimal('5.00'),
                 'budget_category':'Indoor Supplies','category_source':'manual','product_description':'Verified product'},
                {'id':2,'item_name':'Bread','line_total':Decimal('3.00')},
                {'id':3,'item_name':'Bread','line_total':Decimal('3.00')},
                {'id':4,'item_name':'Old OCR row','line_total':Decimal('2.00')}]
    receipt = SimpleNamespace(merchant='Example Shop',items=[ingest.ScannedItem('Mystery product',5),
        ingest.ScannedItem('Bread',3),ingest.ScannedItem('Bread',3),ingest.ScannedItem('New product',2,'BAKERY')])
    assert not receipt_items.unmatched_reviewed(receipt.items,existing)
    receipt_items.persist_items(cur,receipt,9,existing,'budget')
    inserts = [(sql,params) for sql,params in cur.calls if 'INSERT INTO' in sql]
    assert len(inserts) == 1 and inserts[0][1][1] == 'New product'
    updates = [params for sql,params in cur.calls if 'UPDATE' in sql]
    assert {params[-1] for params in updates} == {2,3,101}
    assert not any(params[-1] == 1 for params in updates)
    deletes = [params for sql,params in cur.calls if 'DELETE' in sql]
    assert deletes == [(9,[4])]


def test_unmatched_reviewed_items_block_automatic_replacement():
    existing = [{'id':1,'item_name':'Verified product','line_total':Decimal('5.00'),'category_source':'manual'}]
    assert receipt_items.unmatched_reviewed([ingest.ScannedItem('Different OCR reading',5)],existing)
    assert receipt_items.unmatched_reviewed([ingest.ScannedItem('Verified product',6)],existing)
    assert not receipt_items.unmatched_reviewed([ingest.ScannedItem('Verified product',5)],existing)
    duplicate = [{'id':2,'item_name':'Verified product','line_total':5}, *existing]
    assert not receipt_items.unmatched_reviewed([ingest.ScannedItem('Verified product',5)],duplicate)


def test_scanned_persistence_invokes_rules_and_preserves_unmatched_review(monkeypatch):
    receipt = ingest.ScannedReceipt('example.png','example.png','a'*64,'Example Shop',None,None,5,0,5,
                                   items=[ingest.ScannedItem('Bread',5)])
    monkeypatch.setattr(ingest,'resolve_owner_from_db',lambda *a: None)
    class Connection(Cursor):
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self,*a): pass
    conn = Connection()
    called = []
    monkeypatch.setattr(receipt_items,'load_existing',lambda *a: (7,[]))
    monkeypatch.setattr(receipt_items,'persist_items',lambda *a: called.append(a))
    assert ingest.upsert_receipt(conn,receipt) == 101
    assert called and called[0][1] is receipt
    monkeypatch.setattr(receipt_items,'load_existing',lambda *a: (7,[{'id':1,'item_name':'Reviewed', 'line_total':5,'category_source':'manual'}]))
    before = len(conn.calls)
    assert ingest.upsert_receipt(conn,receipt) == 7
    assert len(conn.calls) == before
