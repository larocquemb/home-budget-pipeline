"""Retailer item identity is separate from description and financial allocation."""
import copy
import pytest
from home_budget_pipeline.receipts.item_codes import item_identity
from home_budget_pipeline.product_enrichment import candidate_evidence, product_query
from home_budget_pipeline.web.queries import LedgerQueryService


@pytest.mark.parametrize('reading,code,label',[
    ('321 5 DOZ EGGS','321','5 DOZ EGGS'),
    ('1234567 VASELINE DSR','1234567','VASELINE DSR'),
    ('001281 1% MILK','001281','1% MILK'),
    ('98765432 CETAPHIL','98765432','CETAPHIL'),
])
def test_costco_prefix_is_a_code_not_part_of_product_name(reading,code,label):
    row={'item_name':reading,'line_total':12.34}
    before=copy.deepcopy(row)
    assert item_identity(row,'Costco')=={'product_code':code,'receipt_item_name':label,'discount_for_product_code':None}
    assert row==before


def test_other_merchant_numeric_names_are_not_reinterpreted():
    assert item_identity({'item_name':'500 ML WATER'},'ExampleMart')['product_code'] is None
    assert item_identity({'item_name':'2 X MILK'},'Costco')['product_code'] is None
    assert item_identity({'item_name':'500 ML WATER'},'Costco')['product_code'] is None


def test_existing_separate_sku_supported():
    assert item_identity({'item_name':'MILK','sku':'12345'},'Costco')['product_code']=='12345'


def test_discount_own_code_and_referenced_product_are_distinct():
    result=item_identity({'item_name':'7654321 TPD/1234567','line_total':-4},'Costco')
    assert result=={'product_code':'7654321','receipt_item_name':'TPD/1234567','discount_for_product_code':'1234567'}
    assert product_query('7654321 TPD/1234567','Costco') is None
    assert product_query('87654321 TPD/1234567','Costco') is None
    evidence=candidate_evidence('87654321 TPD/1234567','costco.ca',
        'Vaseline Item #87654321','https://www.costco.ca/vaseline.product.87654321.html')
    assert evidence['confidence']==0 and evidence['reason']=='discount_adjustment'


def test_matching_searches_code_and_name_together():
    assert product_query('321 5 DOZ EGGS','Costco')=='site:costco.ca "321" 5 DOZ EGGS'
    assert product_query('001281 1% MILK','Costco')=='site:costco.ca "001281" 1% MILK'


def test_matching_code_requires_product_name_and_product_page_evidence():
    url='https://www.costco.ca/lotion.product.100000.html'
    exact=candidate_evidence('1234567 CETAPHIL','costco.ca','Cetaphil Moisturizing Lotion Item #1234567',url)
    assert exact['product_code']=='1234567' and exact['product_code_matched'] is True
    assert exact['confidence']==.95
    unrelated=candidate_evidence('1234567 CETAPHIL','costco.ca','Eggs Item #1234567',url)
    assert unrelated['confidence']<.9
    conflict=candidate_evidence('1234567 CETAPHIL','costco.ca','Cetaphil Lotion Item #7654321',url)
    assert conflict['product_code_conflict'] is True and conflict['confidence']==0
    category=candidate_evidence('1234567 CETAPHIL','costco.ca','Cetaphil Item #1234567','https://www.costco.ca/categories/lotion')
    assert category['confidence']==0


def test_api_adds_separate_identity_without_rewriting_stored_reading():
    stored={'expense_item_id':1,'item_name':'321 5 DOZ EGGS','line_total':19.89}
    class Service(LedgerQueryService):
        def _fetch(self,sql,params=()):
            if 'analytics_expenses a' in sql:return ({'expense_pk':1,'store_name':'Costco'},)
            if 'analytics_expense_items a' in sql:return (stored,)
            return ()
    result=Service().expense_detail(1)
    assert result['items'][0]['product_code']=='321'
    assert result['items'][0]['receipt_item_name']=='5 DOZ EGGS'
    assert result['items'][0]['item_name']==stored['item_name']
    assert 'product_code' not in stored


def test_legacy_electronic_metadata_requires_exact_item_amount_and_unambiguous_code():
    from home_budget_pipeline.receipts.item_codes import matching_item_name
    item={'item_name':'CETAPHIL','line_total':19.99}
    sources=[{'item':'CETAPHIL','amount_cad':19.99,'sku':'0123456'}]
    assert item_identity(item,'Costco',sources)['product_code']=='0123456'
    assert matching_item_name(item,'Costco',sources)=='0123456 CETAPHIL'
    assert item_identity({**item,'line_total':9.99},'Costco',sources)['product_code'] is None
    assert item_identity(item,'Costco',sources+[{**sources[0],'sku':'7654321'}])['product_code'] is None
