import json
from unittest.mock import MagicMock

from home_budget_pipeline.receipts import ingest,parallel_ingest


def test_source_title_supplies_missing_retailer_without_changing_dates(tmp_path,monkeypatch):
    import pypdfium2
    source=tmp_path/'receipt.pdf';source.write_bytes(b'original PDF')
    document=MagicMock();document.__enter__.return_value.get_metadata_dict.return_value={
        'Title':'Orders & Purchases | ExampleMart','CreationDate':'D:20301225100000'}
    monkeypatch.setattr(pypdfium2,'PdfDocument',lambda *a:document)
    cache=tmp_path/'cache';cache.mkdir()
    (cache/'receipt.pdf.json').write_text(json.dumps({
        'metadata':{'source_sha256':ingest.sha256_file(source),'cache_version':1},
        'page_text':['NORTHVILLE #987\n04/07/2025 11:30\nMilk 10.00\nSUBTOTAL 10.00\nTAX .50\nTOTAL 10.50']}))
    monkeypatch.setattr(ingest,'extract_page_text',lambda *a: (_ for _ in ()).throw(AssertionError('ran OCR')))
    receipt,hit=parallel_ingest._parse_scan_cached(source,tmp_path,cache,False,require_cache=True)
    assert hit and receipt.merchant=='ExampleMart' and receipt.store=='NORTHVILLE #987'
    assert receipt.transaction_date=='2025-04-07'


def test_uninformative_pdf_title_is_not_a_merchant(tmp_path,monkeypatch):
    import pypdfium2
    document=MagicMock();document.__enter__.return_value.get_metadata_dict.return_value={'Title':'Order Summary'}
    monkeypatch.setattr(pypdfium2,'PdfDocument',lambda *a:document)
    assert ingest.pdf_merchant_hint(tmp_path/'receipt.pdf') is None


def test_email_subject_names_issuer_and_never_recipient_address():
    text='SSSSuuuubbbbjjjjeeeecccctttt::::Your 7Example Entertainment GmbH receipt [#1234]\nTTTToooo:::: customer@example.test\nTOTAL 40.00'
    assert ingest.extract_merchant(text)=='7Example Entertainment GmbH'
    assert ingest.extract_merchant('TTTToooo:::: customer@example.test\nTOTAL 40.00') is None
    assert ingest.extract_merchant('Order Summary\nShip to\nCustomer Person\nTOTAL 40.00') is None


def test_explicit_issuer_in_filename_survives_missing_pdf_metadata(tmp_path,monkeypatch):
    import pypdfium2
    document=MagicMock();document.__enter__.return_value.get_metadata_dict.return_value={}
    monkeypatch.setattr(pypdfium2,'PdfDocument',lambda *a:document)
    assert ingest.pdf_merchant_hint(tmp_path/'7Example Entertainment GmbH receipt [#1234].pdf')=='7Example Entertainment GmbH'
    assert ingest.pdf_merchant_hint(tmp_path/'receipt_0004.pdf') is None
    assert ingest.pdf_merchant_hint(tmp_path/'cash receipt.pdf') is None
