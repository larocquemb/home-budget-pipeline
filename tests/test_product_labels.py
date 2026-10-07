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
    assert text.startswith('<span>Cep Pic Med</span>')
    assert 'Proposed interpretation' in text and 'Oep Pic Med' in text
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
    assert 'Related OCR observations (2 readings)' in text and 'Oep Pic Med' in text
    assert 'engine: paddle' in text and 'pass id: 3' in text
    assert 'Proposed interpretation' not in text
    assert ocr_variants({}) == []


def test_accepted_interpretation_leads_with_imported_text_in_collapsed_history():
    item = {'item_name': 'Cep Pic Med', 'recommendation': {
        'accepted_at': '2026-10-06T19:59:19Z',
        'evidence': {'tokens': [{'token': 'cep', 'matched': 'oep', 'kind': 'ocr_brand_initialism'}]},
        'ocr_variants': [{'reading': 'Oep Pic Med', 'observations': [
            {'text': 'Oep Pic Med', 'engine': 'tesseract', 'pass_id': 10, 'line_number': 2}]}]}}
    text = _item_reading_html(item)
    assert text.startswith('<strong>Oep Pic Med</strong><br><small>Accepted interpretation</small>')
    assert '<details><summary>Import history</summary><small>Text selected during import</small><br>Cep Pic Med</details>' in text
    assert '<details open' not in text
    assert 'Related OCR observations' in text and 'Row association unverified' in text
    assert 'Stored item reading' not in text
    assert item['item_name'] == 'Cep Pic Med'


def test_existing_accepted_raw_titles_display_cleanly_without_rewriting_manual_descriptions():
    raw='Buy Salsa Medium 650 ml | Sobeys Inc.'
    item={'product_description':raw,'product_url':'https://www.sobeys.com/products/salsa',
          'enrichment':{'candidate_title':raw,'candidate_url':'https://www.sobeys.com/products/salsa'}}
    assert _item_description(item)=='Salsa Medium 650 ml'
    item['product_description']='Buy snacks for the picnic'
    assert _item_description(item)=='Buy snacks for the picnic'


def test_duplicate_item_rows_do_not_turn_pass_line_numbers_into_confirmed_item_identity():
    payload = {'evidence_bundle': {'sources': [
        {'id': 'pass:run-1:10:line:2', 'kind': 'ocr_pass', 'engine': 'tesseract',
         'pass_id': 10, 'ocr_run_uuid': 'run-1', 'line_number': 2, 'page_number': 1, 'text': 'Oep Pic Med'},
        {'id': 'pass:run-1:10:line:3', 'kind': 'ocr_pass', 'engine': 'tesseract',
         'pass_id': 10, 'ocr_run_uuid': 'run-1', 'line_number': 3, 'page_number': 1, 'text': 'Cep Pic Med'}]}}
    variants = ocr_variants(payload)
    assert {o['line_number'] for v in variants for o in v['observations']} == {2, 3}
    assert all(o['row_association'] == 'unverified' for v in variants for o in v['observations'])
    for item_id in [4300, 4301]:
        text = _item_reading_html({'expense_item_id': item_id, 'item_name': 'Cep Pic Med',
                                  'recommendation': {'ocr_variants': variants}})
        assert 'Row association unverified' in text
        assert 'OCR output line: 2' in text and 'OCR output line: 3' in text
        assert 'OCR output line numbers are not Ledger item numbers' in text
        assert 'Original OCR' not in text and 'Recorded OCR variants' not in text


def test_text_retrieval_and_legacy_prompt_metadata_explicitly_retain_row_uncertainty():
    from home_budget_pipeline.receipt_shared_evidence import relevant_lines
    from home_budget_pipeline.receipt_collaboration import prompt_observation
    lines = relevant_lines('Oep Pic Med\nCep Pic Med', 'Cep Pic Med', 'sobeys.com')
    assert len(lines) == 2
    assert all(line['row_association'] == 'unverified' for line in lines)
    assert all(line['association_method'] == 'text_similarity' for line in lines)
    for kind in ['ocr_pass', 'ocr_consensus', 'ocr_layout']:
        observation = prompt_observation({'id': 'old-observation', 'kind': kind,
                                         'line_number': 2, 'pass_id': 10})
        assert observation['row_association'] == 'unverified'
        assert observation['line_number'] == 2 and observation['pass_id'] == 10
    assert 'row_association' not in prompt_observation({'id': 'item:canonical', 'kind': 'canonical_item'})
