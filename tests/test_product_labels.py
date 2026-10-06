from home_budget_pipeline.product_labels import product_name, corrected_reading, ocr_variants
from home_budget_pipeline.web.receipt_app import _item_reading_html, _item_description


def test_product_label_omits_shopping_prompts_and_matching_retailer_suffix_only():
    title = 'Buy Old El Paso Salsa Picante Style Restaurant Medium 650 ml | Sobeys Inc.'
    assert product_name(title, 'https://www.sobeys.com/products/salsa') == 'Old El Paso Salsa Picante Style Restaurant Medium 650 ml'
    for verb in ['Buy', 'Shop', 'Purchase', 'Order', 'Shop now']:
        assert product_name(verb+' Salsa Medium 650 ml') == 'Salsa Medium 650 ml'
    assert product_name("Buyer's Choice Salsa") == "Buyer's Choice Salsa"
    assert product_name('Buy Salsa | Two Flavours', 'https://www.sobeys.com/products/salsa') == 'Salsa | Two Flavours'
    assert product_name('Buy Salsa | Costco', 'https://www.sobeys.com/products/salsa') == 'Salsa | Costco'


def test_correction_uses_explicit_scored_initialism_and_keeps_original_observations():
    evidence = {'tokens': [{'token': 'cep', 'matched': 'oep', 'kind': 'ocr_brand_initialism'}]}
    assert corrected_reading('Cep Pic Med', evidence) == 'Oep Pic Med'
    assert corrected_reading('Other item', evidence) is None
    assert corrected_reading('Cep Pic Med', {'tokens':[{'token':'cep','matched':'oep','kind':'unmatched'}]}) is None
    assert corrected_reading('Cep Pic Med', None) is None
    item = {'item_name':'Cep Pic Med', 'recommendation':{'eligible':True,'evidence':evidence}}
    text = _item_reading_html(item)
    assert 'Original OCR' in text and 'Cep Pic Med' in text and 'Proposed corrected reading' in text and 'Oep Pic Med' in text
    assert item['item_name'] == 'Cep Pic Med'


def test_ocr_variants_show_only_real_observations_and_their_provenance():
    payload = {'evidence_bundle':{'sources':[
        {'id':'canonical','kind':'canonical_item','text':'Cep Pic Med'},
        {'id':'t1','kind':'ocr_pass','text':'Cep Pic Med $6.49 C','engine':'tesseract','pass_id':1},
        {'id':'t2','kind':'ocr_pass','text':'Oep Pic Med','engine':'tesseract','pass_id':2},
        {'id':'p1','kind':'ocr_pass','text':'Oep Pic Med','engine':'paddle','pass_id':3}]},
        'reading_hypotheses':[{'reading':'Cep Pic Med','source_ids':['canonical','t1']},
                              {'reading':'Oep Pic Med','source_ids':['t2','p1']},
                              {'reading':'Invented model reading','source_ids':[]}]}
    variants = ocr_variants(payload)
    assert [v['reading'] for v in variants] == ['Cep Pic Med','Oep Pic Med']
    assert len(variants[1]['observations']) == 2
    assert variants[0]['observations'][0]['text'] == 'Cep Pic Med $6.49 C'
    text = _item_reading_html({'item_name':'Cep Pic Med','recommendation':{'ocr_variants':variants}})
    assert 'Recorded OCR variants (2)' in text and 'Oep Pic Med' in text
    assert 'engine: paddle' in text and 'pass id: 3' in text
    assert 'Proposed corrected reading' not in text
    assert ocr_variants({}) == []


def test_existing_accepted_raw_titles_display_cleanly_without_rewriting_manual_descriptions():
    raw='Buy Salsa Medium 650 ml | Sobeys Inc.'
    item={'product_description':raw,'product_url':'https://www.sobeys.com/products/salsa',
          'enrichment':{'candidate_title':raw,'candidate_url':'https://www.sobeys.com/products/salsa'}}
    assert _item_description(item)=='Salsa Medium 650 ml'
    item['product_description']='Buy snacks for the picnic'
    assert _item_description(item)=='Buy snacks for the picnic'
