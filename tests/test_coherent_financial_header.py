"""Printed financial headers outrank inferred balancing values."""
from types import SimpleNamespace
import pytest
from home_budget_pipeline.receipts import ingest
from home_budget_pipeline.receipts.financial_header import apply


def observation(text,engine,number):
    return dict(text=text,engine=engine,pass_id=number,page_number=2,run_uuid='synthetic',status='success')


def test_printed_tax_is_not_increased_to_hide_a_subtotal_mismatch():
    assert ingest.extract_totals('SUBTOTAL 81.00\nGST 1.20\nTOTAL 91.20')==(81.00,1.20,91.20)


@pytest.mark.parametrize('tax',['5% CST $1.20','5% CST\n$1.20'])
def test_percentage_header_with_ocr_tax_label_confusion_keeps_printed_amount(tax):
    assert ingest.extract_totals('SUBTOTAL $90.00\n'+tax+'\nTOTAL $91.20',infer_tax=False)==(90.,1.20,91.20)


def test_coherent_original_header_supported_by_two_engines_replaces_bad_summary():
    r=SimpleNamespace(subtotal=890.,tax=1.2,total=91.20,field_decisions={})
    good='SUBTOTAL\n90.00\n5% CST\n1.20\nTOTAL\n91.20'
    apply(r,[observation(good,'paddle',1),observation(good,'tesseract',2)])
    assert (r.subtotal,r.tax,r.total)==(90.,1.20,91.20)
    assert r.field_decisions['financial_header']['status']=='selected'


def test_incoherent_or_competing_headers_are_not_solved_by_choosing_a_target_total():
    r=SimpleNamespace(subtotal=81.,tax=1.2,total=91.20,field_decisions={})
    bad='SUBTOTAL 81.00\nGST 1.20\nTOTAL 91.20'
    apply(r,[observation(bad,'paddle',1),observation(bad,'tesseract',2)])
    assert r.subtotal==81. and not r.field_decisions
    a='SUBTOTAL 90.00\nGST 1.20\nTOTAL 91.20'
    b='SUBTOTAL 91.00\nGST 1.20\nTOTAL 92.20'
    apply(r,[observation(t,e,i) for i,(t,e) in enumerate([(a,'paddle'),(a,'tesseract'),(b,'paddle'),(b,'tesseract')])])
    assert r.subtotal==81. and not r.field_decisions
