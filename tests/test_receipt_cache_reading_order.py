"""Section nesting must not relocate rows with missing layout evidence."""
from home_budget_pipeline.receipts.parallel_ingest import _cached_pages,_line_texts
from home_budget_pipeline.receipts.item_evidence import rows


def test_nested_cache_restores_original_item_and_adjustment_order():
    lines=[{'line_number':1,'text':'GROCERY','children':[
        {'line_number':2,'text':'Berry Drink $7.40','children':[]},
        {'line_number':5,'text':'Rice $3.20','children':[]}]},
        {'line_number':3,'text':'INSTANT SAVINGS -$0.60'},
        {'line_number':4,'text':'Kitchen Cleaner $8.10'}]
    data={'metadata':{'cache_version':4},'pages':[{'page_number':1,'lines':lines}]}
    text=_cached_pages(data)[0]
    assert text.splitlines()==['GROCERY','Berry Drink $7.40','INSTANT SAVINGS -$0.60','Kitchen Cleaner $8.10','Rice $3.20']
    assert [(r['name'],float(r['amount'])) for r in rows(text)]==[('Berry Drink',6.80),('Kitchen Cleaner',8.10),('Rice',3.20)]
    # Reading must leave the recorded tree and original line numbers untouched.
    assert lines[1]['line_number']==3 and len(lines[0]['children'])==2


def test_legacy_or_ambiguous_line_numbers_keep_traversal_order():
    assert _line_texts(['First',{'text':'Second','children':[{'text':'Third'}]}])==['First','Second','Third']
    assert _line_texts([{'line_number':2,'text':'First'},{'line_number':2,'text':'Second'}])==['First','Second']
