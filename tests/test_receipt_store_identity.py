import copy

import pytest

from home_budget_pipeline.receipts.ingest import extract_merchant_store
from home_budget_pipeline.receipts.identities import identity_plan
from home_budget_pipeline.receipts.payment import extract_payment_provenance
from home_budget_pipeline.receipts.ocr_results import OcrResultMessage, result_messages_for_receipt, receipt_from_message
from test_ocr_results_collector import result_receipt


@pytest.mark.parametrize('text,hint,expected', [
    ('Orders & Purchases | ExampleMart\nNORTHVILLE #987\nhttps://examplemart.test/receipts', None, ('ExampleMart', 'NORTHVILLE #987')),
    ('NORTHVILLE #987\n500 Example Rd\nCOSTCO MASTERCARD 12.00', None, (None, 'NORTHVILLE #987')),
    ('NORTHVILLE #987\n500 Example Rd', 'ExampleMart', ('ExampleMart', 'NORTHVILLE #987')),
    ('NORTHVILLE #987', 'NORTHVILLE #987', (None, 'NORTHVILLE #987')),
    ('maurices\nStore: 1234 Register: 2', None, ('maurices', 'Store 1234')),
    ('Sobeys Exampleville', None, ('Sobeys', 'Exampleville')),
    ('EXAMPLE MARKET\nStore 1234\nST# 01234 OP# 123', None, ('EXAMPLE MARKET', 'Store 1234')),
    ('EXAMPLE MARKET\nStore 1234\nST# 5678 OP# 123', None, ('EXAMPLE MARKET', None)),
])
def test_retailer_and_printed_store_are_separate(text, hint, expected):
    assert extract_merchant_store(text, hint) == expected


def row():
    return {'id': 1, 'source_sha256': 'a'*64, 'source_reference': 'receipt.pdf',
            'raw_text': 'NORTHVILLE #987\nCHIP\nXXXXXXXX4321\nMastercard 12.00',
            'merchant': 'NORTHVILLE #987', 'merchant_hint': 'ExampleMart',
            'raw_payload': {}, 'receipt_id': 'old-id', 'payment_method': 'Mastercard',
            'card_last4': None}


def test_identity_plan_is_pure_preserves_ids_and_is_idempotent():
    before = row()
    snapshot = copy.deepcopy(before)
    plan = identity_plan([before])
    change, = plan['changes']
    assert change['after'] == {'merchant':'ExampleMart', 'store':'NORTHVILLE #987',
                               'receipt_id':'old-id','payment_method':'Mastercard','card_last4':'4321'}
    assert before == snapshot
    after = {**before, **change['after'], 'raw_payload': {'store':change['after']['store']}}
    assert identity_plan([after])['changes'] == []
    stale = {**before, 'raw_text':before['raw_text']+'\nNew evidence'}
    assert identity_plan([stale])['version'] != plan['version']


def test_identity_refresh_respects_audited_field_corrections():
    before = row()
    before['feedback_fields'] = [{'merchant':'verified merchant','card_last4':'verified card'}]
    change, = identity_plan([before])['changes']
    assert change['after']['merchant'] == before['merchant']
    assert change['after']['card_last4'] is None


@pytest.mark.parametrize('label,last4', [('CHIP','4321'),('Contactless','4321'),('Member',None),('Loyalty',None),('',None)])
def test_standalone_mask_requires_card_entry_context(label,last4):
    assert extract_payment_provenance(label+'\nXXXXXXXX4321\nMastercard 12.00').card_last4 == last4


def test_store_result_round_trip_and_old_results_remain_readable(tmp_path):
    path = tmp_path/'receipt.pdf'
    path.write_bytes(b'synthetic')
    receipt = result_receipt(path)
    receipt.store = 'NORTHVILLE #987'
    message = result_messages_for_receipt(receipt)[0]
    assert receipt_from_message(OcrResultMessage.from_bytes(message.to_bytes())).store == receipt.store
    legacy = {**message.payload, 'receipt': {k:v for k,v in message.payload['receipt'].items() if k!='store'}}
    from dataclasses import replace
    assert receipt_from_message(replace(message,payload=legacy)).store is None


def test_identity_plan_uses_retained_passes_and_versions_its_evidence():
    before=row()
    before.update(raw_text='EXAMPLE CENTRE 1234 SAMPLE ROAD\nTOTAL 12.00',merchant='EXAMPLE CENTRE',merchant_hint=None)
    text='AB&CD\nVisit our website at www.ab-cd.com\nTOTAL 12.00'
    before['ocr_observations']=[{'text':text,'page_number':1,'status':'success','pass_id':n} for n in (1,2)]
    plan=identity_plan([before])
    assert plan['changes'][0]['after']['merchant']=='AB&CD'
    assert plan['changes'][0]['ocr_observations_sha256']
    altered=copy.deepcopy(before);altered['ocr_observations'][0]['pass_id']=3
    assert identity_plan([altered])['version']!=plan['version']
    before['feedback_fields']=[{'merchant':'Image verified'}]
    assert all(c['after']['merchant']=='EXAMPLE CENTRE' for c in identity_plan([before])['changes'])
