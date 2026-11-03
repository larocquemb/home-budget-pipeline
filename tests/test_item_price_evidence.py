"""Price choices require original row evidence; no subtotal is an input."""
import json
import pytest

from home_budget_pipeline.receipts.item_evidence import resolve,rows


def observation(text,engine,number,*,confidence=98,page=1,**kwargs):
    return dict(text=text,engine=engine,pass_id=number,page_number=page,
        run_uuid='synthetic',status='success',dpi=200,psm='6',variant='raw',
        provenance={'lines':[dict(line_number=i+1,text=l,confidence=confidence)
                             for i,l in enumerate(text.splitlines())]},**kwargs)


def test_misread_price_replaced_from_two_engines_not_a_subtotal_guess():
    base='Greek Yogurt 12Pk 68.40\nBlueberries $3.20'
    clear='Greek Yogurt 12Pk $8.40\nBlueberries $3.20'
    items,decisions=resolve([base],[observation(clear,'paddle',1),observation(clear,'tesseract',2)])
    assert [i.line_total for i in items]==[8.40,3.20]
    assert decisions[0]['original_value']=='68.4'
    assert decisions[0]['reason']=='independent_engines_agree'
    assert {c['source']['engine'] for c in decisions[0]['candidates']}=={'paddle','tesseract'}
    assert {c['source']['line_number'] for c in decisions[0]['candidates']}=={1}


def test_split_columns_keep_repeat_purchases_and_original_price_lines():
    text='Greek Yogurt\n$8.40\nYOU SAVED $1.20\n$2.30\nBerry Pint\n1 @ 2/$4.60\nYOU SAVED $2.00\n$2.30\nBerry Pint'
    result=rows(text)
    assert [(r['name'],float(r['amount'])) for r in result]==[('Greek Yogurt',8.40),('Berry Pint',2.30),('Berry Pint',2.30)]
    assert [r['money_line_number'] for r in result]==[2,4,8]


def test_ambiguous_missing_price_is_not_borrowed_between_products():
    assert rows('First Sauce\n$5.20\nSecond Sauce')==[]
    assert float(rows('First Sauce\n$5.20\nSecond Sauce\n$6.20')[1]['amount'])==6.20


def test_later_priced_repeat_is_not_attributed_to_an_earlier_unpriced_product():
    text='First Sauce $4.20\nBerry Pint\n1 @ 2/$4.60\nYOU SAVED $1.00\nBerry Pint $2.30\nBread Rolls $3.20'
    result=rows(text,placeholders=True)
    assert [(r['line_number'],float(r['amount']) if r['amount'] is not None else None) for r in result]==[(1,4.2),(2,None),(5,2.3),(6,3.2)]
    good='First Sauce $4.20\nBerry Pint $2.30\nBerry Pint $2.30\nBread Rolls $3.20'
    items,decisions=resolve([text],[observation(good,'paddle',1),observation(good,'tesseract',2)])
    assert [i.line_total for i in items]==[4.20,2.30,2.30,3.20]
    assert [d['line_number'] for d in decisions]==[1,2,5,6]


def test_separate_price_tax_marker_does_not_break_the_next_discount():
    text='First Sauce\n$5.20\nC\nINSTANT SAVINGS\n-$0.60\nBread Rolls\n$3.20'
    assert [(r['name'],float(r['amount'])) for r in rows(text)]==[('First Sauce',4.60),('Bread Rolls',3.20)]


def test_missing_duplicate_purchase_is_not_assigned_to_both():
    items,decisions=resolve(['Berry Pint $62.30\nBerry Pint $62.30'],
        [observation('Berry Pint $2.30','paddle',1),observation('Berry Pint $2.30','tesseract',2)])
    assert [i.line_total for i in items]==[62.30,62.30]
    assert all(not d['candidates'] for d in decisions)


def test_repeated_passes_from_one_engine_do_not_outvote_conflicting_engines():
    base='Greek Yogurt $8.40'
    observations=[observation('Greek Yogurt $9.40','tesseract',i,confidence=94) for i in range(1,8)]
    observations+=[observation(base,'paddle',9,confidence=94)]
    items,decisions=resolve([base],observations)
    assert items[0].line_total==8.40 and decisions[0]['status']=='conflict'


def test_two_independently_supported_prices_stay_in_review():
    observations=[observation(f'Greek Yogurt ${v}',e,i) for i,(v,e) in enumerate(
        [('8.40','paddle'),('8.40','tesseract'),('9.40','paddle'),('9.40','tesseract')])]
    items,ds=resolve(['Greek Yogurt $8.40'],observations)
    assert ds[0]['status']=='conflict' and items[0].line_total==8.40


@pytest.mark.parametrize('gap,selected',[(14,False),(15,True),(25,True)])
def test_price_confidence_margin_is_explicit_and_engine_neutral(gap,selected):
    for first,second in [('paddle','tesseract'),('tesseract','paddle')]:
        items,ds=resolve(['Greek Yogurt $9.40'],[
            observation('Greek Yogurt $8.40',first,1,confidence=95),
            observation('Greek Yogurt $9.40',second,2,confidence=95-gap)])
        assert items[0].line_total==(8.40 if selected else 9.40)
        assert ds[0]['status']==('selected' if selected else 'conflict')


def test_neighbour_anchors_recover_typo_and_missing_price_without_switching_products():
    base='First Sauce $4.20\nStrawbex 250G $62.30\nCheddar Cheese\nLast Product $7.00'
    good='First Sauce $4.20\nStrawberries 250G $2.30\nCheddar Cheese $5.00\nLast Product $7.00'
    items,ds=resolve([base],[observation(good,'paddle',1),observation(good,'tesseract',2)])
    assert [i.line_total for i in items]==[4.20,2.30,5.00,7.00]
    assert ds[2]['original_value'] is None


def test_different_product_size_cannot_supply_price():
    _,ds=resolve(['Greek Yogurt 500G $9.40'],[
        observation('Greek Yogurt 900G $8.40','paddle',1),observation('Greek Yogurt 900G $8.40','tesseract',2)])
    assert ds[0]['value']=='9.4' and not ds[0]['candidates']


def test_page_boundary_overlap_keeps_later_discount_and_both_sources():
    pages=['Roasted Ham $6.20\nCheddar Dip $4.80',
           'Roasted Ham $6.20\nCheddar Dip $4.80\nINSTANT SAVINGS $0.60\nBread Rolls $3.20\nSUBTOTAL $13.60']
    observations=[observation(p,e,i,page=n) for i,(n,p,e) in enumerate(
        [(1,pages[0],'paddle'),(1,pages[0],'tesseract'),(2,pages[1],'paddle'),(2,pages[1],'tesseract')])]
    items,ds=resolve(pages,observations)
    assert [i.line_total for i in items]==[6.20,4.20,3.20]
    assert sum(not d['included'] for d in ds)==2
    assert len([d for d in ds if d.get('overlap_sources')])==2
    assert all('candidates' in source for d in ds for source in d.get('overlap_sources',[]))


def test_single_repeated_product_at_boundary_is_not_assumed_an_overlap():
    pages=['Greek Yogurt $8.40','Greek Yogurt $8.40']
    obs=[observation(p,e,i,page=n) for i,(n,p,e) in enumerate(
        [(1,pages[0],'paddle'),(1,pages[0],'tesseract'),(2,pages[1],'paddle'),(2,pages[1],'tesseract')])]
    items,_=resolve(pages,obs)
    assert len(items)==2


def test_retained_geometry_pairs_price_column_without_adjacent_line_guess():
    text='Greek Yogurt\nBerry Pint\n$8.40\n$2.30'
    provenance={'lines':[dict(line_number=i+1,x=x,y=y,width=w,height=12) for i,(x,y,w) in enumerate(
        [(10,10,90),(10,35,90),(140,10,40),(140,35,40)])]}
    assert [(r['name'],float(r['amount'])) for r in rows(text,provenance=provenance)]==[('Greek Yogurt',8.40),('Berry Pint',2.30)]


def test_adjacent_product_does_not_steal_split_discount_or_savings_summary():
    text='First Sauce $5.20\nINSTANT SAVINGS\n-$0.60\nYOU SAVED\n$1.20\nSecond Sauce\n$6.20'
    assert [(r['name'],float(r['amount'])) for r in rows(text)]==[('First Sauce',4.60),('Second Sauce',6.20)]


def test_source_without_recognition_geometry_does_not_invent_bounds():
    from home_budget_pipeline.receipts.paddle_geometry import bounds
    assert bounds([],0)==(None,)*4
    assert bounds([[[1,float('nan')]]*4],0)==(None,)*4
    assert bounds([[[1,2],[11,2],[11,7],[1,7]]],0)==(1.,2.,10.,5.)


def test_paddle_candidate_keeps_polygons_and_confidence(monkeypatch):
    from types import SimpleNamespace
    from PIL import Image
    from home_budget_pipeline.receipts import ingest
    result={'rec_texts':['Greek Yogurt','$8.40'],'rec_scores':[.98,.96],
            'rec_polys':[[[10,20],[90,20],[90,30],[10,30]],[[120,20],[160,20],[160,30],[120,30]]]}
    fake=SimpleNamespace(predict=lambda *_:[SimpleNamespace(json={'res':result})])
    monkeypatch.setattr(ingest,'_PADDLE_OCR',fake)
    monkeypatch.setattr(ingest,'_paddle_ocr_enabled',lambda:True)
    candidate=ingest._run_paddle_candidate(Image.new('RGB',(200,100)),dpi=200)
    assert candidate.status=='success'
    assert [(line.x,line.y,line.width,line.height) for line in candidate.lines]==[(10,20,80,10),(120,20,40,10)]
    metric=ingest._ocr_pass_metric(candidate,page=1,variant='raw',seconds=1)
    assert metric['provenance']['lines'][1]['x']==120
    assert metric['provenance']['lines'][1]['confidence']==96


def test_direct_and_cached_paths_publish_identical_item_decisions(tmp_path,monkeypatch):
    from home_budget_pipeline.receipts import ingest,parallel_ingest
    from home_budget_pipeline.receipts.ocr_results import result_messages_for_receipt,OcrResultMessage,receipt_from_message
    source=tmp_path/'receipt.png';source.write_bytes(b'synthetic source')
    text='EXAMPLE SHOP\nDATE 07/12/2031\nGreek Yogurt 68.40\nSUBTOTAL 8.40\nGST 0.42\nTOTAL 8.82'
    good=text.replace('68.40','$8.40')
    obs=[observation(good,'paddle',1),observation(good,'tesseract',2)]
    for p in obs:p['run_uuid']='11111111-1111-4111-8111-111111111111'
    def extract(path,metrics):metrics.extend(obs);return [text]
    monkeypatch.setattr(ingest,'extract_page_text',extract)
    monkeypatch.setattr(ingest,'extract_page_layout',lambda *a:[])
    direct=ingest.parse_scan(source,tmp_path)
    monkeypatch.setattr(parallel_ingest,'_read_or_create_pages',lambda *a,**k:([text],True))
    monkeypatch.setattr(parallel_ingest,'_ocr_run_metadata',lambda *a:{'ocr_passes':obs,'run_uuid':obs[0]['run_uuid']})
    monkeypatch.setattr(parallel_ingest,'_structured_layout',lambda *a:[])
    cached,_=parallel_ingest._parse_scan_cached(source,tmp_path,tmp_path/'cache',False)
    assert direct.items==cached.items
    assert direct.field_decisions==cached.field_decisions
    assert cached.items[0].line_total==8.40
    assert cached.text==text and source.read_bytes()==b'synthetic source'
    # Validate the actual run-event wire contract, without sending to RabbitMQ.
    cached.ocr_run['ocr_passes']=[]
    message=result_messages_for_receipt(cached)[0]
    decoded=OcrResultMessage.from_bytes(message.to_bytes())
    restored=receipt_from_message(decoded)
    assert restored.field_decisions==cached.field_decisions
    assert restored.items[0].line_total==8.40


def test_unrelated_runs_cannot_jointly_corroborate_a_price():
    a=observation('Greek Yogurt $8.40','paddle',1,confidence=94)
    b=observation('Greek Yogurt $8.40','tesseract',2,confidence=94)
    b['run_uuid']='another-run'
    items,ds=resolve(['Greek Yogurt $9.40'],[a,b])
    assert items[0].line_total==9.40 and ds[0]['status']=='conflict'


def test_price_at_section_boundary_cannot_be_borrowed_by_next_product():
    text='MEAT\nLean Ground Beef\nBAKERY\n$12.80 C\nBread Rolls\nBread Rolls\n$4.20'
    assert all(float(r['amount'])!=12.80 for r in rows(text))


def test_consistent_original_price_without_cross_engine_coverage_remains_observed():
    from home_budget_pipeline.receipts import ingest
    from home_budget_pipeline.receipts.item_evidence import apply
    text='Greek Yogurt $8.40'
    r=ingest.ScannedReceipt(path='',source_reference='example.png',source_sha256='a'*64,
        merchant='Example Shop',transaction_date='2031-07-12',receipt_id=None,
        subtotal=8.40,tax=0,total=8.40,text=text)
    apply(r,[text],[observation(text,'tesseract',1)])
    assert r.field_decisions['items']['status']=='selected'
    assert r.field_decisions['items']['rows'][0]['status']=='observed'


def test_weight_times_unit_price_only_corroborates_an_observed_literal_price():
    text='First Sauce $4.20\nFresh Apples $12.08\nLast Product $3.20'
    printed='First Sauce $4.20\nFresh Apples $12.03\nLast Product $3.20'
    column='First Sauce $4.20\nPRODUCE\nFresh Apples\n1.215 kg @ $9.90 / kg\nLast Product $3.20'
    items,ds=resolve([text],[observation(printed,'tesseract',1,confidence=80),observation(column,'paddle',2)])
    assert items[1].line_total==12.03
    assert ds[1]['reason']=='observed_price_corroborated_by_weight_quantity'
    assert any(c['kind']=='quantity_check' and c['source']['money_line_number']==4 for c in ds[1]['candidates'])
    # A calculated value not read anywhere as an item price must not be invented.
    items,ds=resolve([text],[observation(text,'tesseract',1,confidence=80),observation(column,'paddle',2)])
    assert items[1].line_total==12.08
    assert any(not c['eligible'] for c in ds[1]['candidates'] if c['kind']=='quantity_check')


def test_weighted_price_validation_handles_trailing_zero_cents():
    text='First Sauce $4.20\nFresh Apples $6.30\nLast Product $3.20'
    good=text.replace('$6.30','$6.20')
    column='First Sauce $4.20\nFresh Apples\n1.000 kg @ $6.20 / kg\nLast Product $3.20'
    items,_=resolve([text],[observation(good,'tesseract',1,confidence=80),observation(column,'paddle',2)])
    assert items[1].line_total==6.20


def test_price_correction_preserves_an_unambiguous_printed_section():
    from home_budget_pipeline.receipts import ingest
    from home_budget_pipeline.receipts.item_evidence import apply
    text='Greek Yogurt 68.40'
    r=ingest.ScannedReceipt(path='',source_reference='example.png',source_sha256='a'*64,
        merchant='Example Shop',transaction_date='2031-07-12',receipt_id=None,
        subtotal=8.40,tax=0,total=8.40,text=text,
        items=[ingest.ScannedItem('Greek Yogurt',68.40,'DAIRY')])
    apply(r,[text],[observation('Greek Yogurt $8.40','paddle',1),observation('Greek Yogurt $8.40','tesseract',2)])
    assert r.items[0].line_total==8.40 and r.items[0].department=='DAIRY'
