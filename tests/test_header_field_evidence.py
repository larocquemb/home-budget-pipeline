"""Header fields use printed roles and original engines, never retailer overrides."""
from types import SimpleNamespace

from home_budget_pipeline.receipts import ingest
from home_budget_pipeline.receipts.header_evidence import resolve
from home_budget_pipeline.receipts.store_contact import phone_evidence
from home_budget_pipeline.receipts.payment import extract_payment_provenance


def observation(text,engine,number,run='synthetic'):
    return dict(text=text,engine=engine,pass_id=number,run_uuid=run,page_number=1,status='success')


def test_unique_cross_engine_phone_survives_single_engine_digit_errors():
    obs=[observation('Harborlane\n204-555-0123',e,i) for i,e in enumerate(('paddle','tesseract'))]
    obs += [observation('Harborlane\n204-555-0124','tesseract',i+2) for i in range(8)]
    d=phone_evidence('Harborlane',obs)
    assert d['value']=='204-555-0123'
    assert d['reason']=='independent_engines_agree'
    assert {c['source']['engine'] for c in d['candidates']}=={'paddle','tesseract'}
    obs.append(observation('Harborlane\n204-555-0124','paddle',20))
    assert phone_evidence('',obs)['value'] is None


def test_phone_support_cannot_be_combined_across_different_runs():
    obs=[observation('Harborlane\n204-555-0123','paddle',1),
         observation('Harborlane\n204-555-0123','tesseract',2,run='other'),
         observation('Harborlane\n204-555-0124','tesseract',3)]
    assert phone_evidence('',obs)['value'] is None


def test_header_requires_observed_brand_in_two_roles_and_an_independent_branch_reading():
    tsv='5 1 3 1 1 1 581 173 174 67 39.970039 junk '
    obs=[observation('Harborlane\nHsrborlane Riverside\nSO Birch Road\n'+tsv*4,'tesseract',1),
         observation('Garbled\nHarborlane Riverside\n12 Birch Road','tesseract',2),
         observation('Logo\nHarborIane Riverside\n12 Birch R o a d','paddle',3)]
    result=resolve(obs)
    assert result['merchant']['value']=='Harborlane'
    assert result['store']['value']=='Harborlane Riverside'
    assert {c['source']['engine'] for c in result['store']['candidates']}=={'paddle','tesseract'}
    assert ingest.extract_merchant_store('Garbled',observations=obs)==('Harborlane','Harborlane Riverside')
    assert resolve(obs[:2])=={}
    conflicting=[observation('Harborlane\nHarborlane Lakeside\n12 Birch Road',engine,i+4)
                 for i,engine in enumerate(('paddle','tesseract'))]
    assert resolve(obs+conflicting)=={}


def test_tesseract_literal_quote_does_not_swallow_following_tsv_rows(monkeypatch,tmp_path):
    header='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    body='5\t1\t1\t1\t1\t1\t10\t10\t10\t10\t90\t"\n'
    body+='5\t1\t1\t1\t2\t1\t10\t30\t60\t10\t95\tProduct\n'
    body+='5\t1\t1\t1\t2\t2\t90\t30\t30\t10\t95\t$4.20\n'
    monkeypatch.setattr(ingest.shutil,'which',lambda *_:'tesseract')
    monkeypatch.setattr(ingest.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=header+body))
    candidate=ingest._run_tesseract_candidate(tmp_path/'synthetic.png',dpi=200,psm='6')
    assert [l.text for l in candidate.lines]==['"','Product $4.20']
    assert not ingest._looks_like_tesseract_tsv(candidate.text)


def test_terminal_card_label_outranks_card_advertising_and_conflicts_abstain():
    text='Earn points with a Visa Card. Learn more online\nCARD MC\nNO. ************1234'
    value=extract_payment_provenance(text)
    assert (value.payment_method,value.card_last4)==('Mastercard','1234')
    assert extract_payment_provenance(text+'\nCARD VISA').payment_method is None
