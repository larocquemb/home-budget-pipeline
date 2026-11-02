"""Extracted evidence items remain visible before an accounting record exists."""
import json
from types import SimpleNamespace

from home_budget_pipeline.receipts import evidence, ingest
from home_budget_pipeline.web.app import _post_ocr_text


def test_persist_unlinked_items_and_render_extraction(monkeypatch):
    saved = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): saved.append(params)
        def fetchone(self): return (42,)
    for name in ('persist_ocr_lines', 'persist_ocr_run', 'persist_auto_annotations'):
        monkeypatch.setattr(evidence, name, lambda *args: None)
    receipt = SimpleNamespace(
        source_reference='receipt.pdf', source_sha256='a'*64,
        merchant='ExampleMart', receipt_id=None, total=30,
        payment_method=None, card_last4=None, extraction_status='review',
        extraction_confidence=.7, text='12345 FIRST PRODUCT 10.00\n23456 SECOND PRODUCT 20.00',
        page_text=[], items=[ingest.ScannedItem('FIRST PRODUCT', 10), ingest.ScannedItem('SECOND PRODUCT', 20)],
    )
    evidence.upsert_evidence(SimpleNamespace(cursor=Cursor), receipt)
    payload = json.loads(saved[0][-1])
    assert payload['items'] == [{'item_name':'FIRST PRODUCT', 'line_total':10,'department':None},
                                {'item_name':'SECOND PRODUCT', 'line_total':20,'department':None}]
    rendered = _post_ocr_text({'raw_payload':payload}, None)
    assert '- FIRST PRODUCT: 10' in rendered
    assert '- SECOND PRODUCT: 20' in rendered
    assert '(none extracted)' not in rendered
    # Linked, reviewed accounting items still take precedence in the display.
    assert '- VERIFIED: 30' in _post_ocr_text({'raw_payload':payload},
        {'items':[{'item_name':'VERIFIED','line_total':30}]})
