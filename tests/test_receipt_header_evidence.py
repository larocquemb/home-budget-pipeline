"""Merchant and total selection use document evidence, not receipt identities."""
import pytest

from home_budget_pipeline.receipts import ingest
from home_budget_pipeline.receipts.datetime import extract_transaction_datetime


@pytest.mark.parametrize('text,expected', [
    ('Store: 9876 Register: 3\nReceipt\nDate: 2025-04-07', None),
    ('Store\nRegister\nIN-STORE PURCHASES\nPURCHASES / ORDERS', None),
    ('Odd OCR slogan\nWillow Books\nStore: 9876\nThank you for shopping with Willow Books, a local company.', 'Willow Books'),
    ('Make yourself at home\nCO-OP\nNorth Trail Co-op\n500 Example Rd', 'North Trail Co-op'),
    ('Odd OCR slogan\nHilltop Cooperative\n500 Example Rd', 'Hilltop Cooperative'),
    ('Local Market\nThank you for shopping at Another Shop', 'Local Market'),
    ('Local Market\nThank you for shopping at our store', 'Local Market'),
    ('Store: 9876\nGift cards for Another Shop', None),
    ('Store# 9876 Willow Books\n123 Example St', 'Willow Books'),
    ('4/7/25, 5:30 PM Orders & Purchases | Willow Books\nBranch 123\nhttps://www.willowbooks.test/receipts', 'Willow Books'),
    ('x\nPai\nWILLOW BOOKS\nStore: 9876', 'WILLOW BOOKS'),
])
def test_merchant_uses_corroborated_name_not_checkout_metadata(text, expected):
    assert ingest.extract_merchant(text) == expected


def test_final_total_excludes_pretax_purchase_summary_and_tax_rates():
    text = '''TOTALS
Subtotal
$20.00
Total Taxes
$2.40
GST/HST(MB)
5.000%
$1.00
# 123456789RT0001
PST (MB)
7.000%
$1.40
# 123456789MT0002
Total
$22.40
Thank you for shopping
Total Purchased: Pr $20.00
Subtotal $20.00'''
    assert ingest.extract_totals(text) == (20.0, 2.4, 22.4)
    assert ingest.extract_tax_components(text) == {'gst': 1.0, 'pst': 1.4}


def test_tax_rate_join_never_crosses_registration_or_unrelated_rows():
    text = 'GST (MB)\n5.000%\n# 123456789RT0001\n$10.00'
    assert ingest.extract_tax_components(text) == {}


@pytest.mark.parametrize('text,expected', [
    ('Apr 7, 2025, 5:44:12 PM', '2025-04-07T17:44:12'),
    ('04/07/2025 06:34 PM', '2025-04-07T18:34:00'),
    ('Date: 4/7/25 Time; 18:31', '2025-04-07T18:31:00'),
    ('Time: 12:03\nDate: 4/7/25', '2025-04-07T12:03:00'),
    ('DATE 04/07/2025\nTIME 12:28:26', '2025-04-07T12:28:26'),
    ('Invoice Date: 2025-04-07\nPrinted Time: 12:28:26', '2025-04-07'),
    ('Invoice Date: 2025-04-07\nUnrelated message\nTime: 12:28:26', '2025-04-07'),
])
def test_preserves_observed_time_without_using_print_time(text, expected):
    assert extract_transaction_datetime(text) == expected


@pytest.mark.parametrize('name', ['AB&CD', 'A&B', 'H+M', 'AB+CD'])
def test_short_punctuated_retailer_logos_are_valid_names(name):
    assert ingest.extract_merchant('“'+name+'\nStore 123\n500 Example Rd') == name


@pytest.mark.parametrize('header', [
    "EXAMPLE CENTRE 1234 ST. SAMPLE ROAD, UNIT 21\nNORTHVILLE, MB R2M5E5\nTelephone: 204-555-0101",
    "1234 EXAMPLE BLVD\nNORTHVILLE, MB R2M5E5\nReg Price: 19.%\nGroup Discount -15%",
])
def test_address_and_contact_metadata_cannot_become_merchant(header):
    assert ingest.extract_merchant(header) is None


def observation(text,page=1,status='success'):
    return {'text':text,'page_number':page,'status':status}


def test_alternative_headers_require_repeated_name_and_matching_printed_domain():
    primary='EXAMPLE CENTRE 1234 SAMPLE ROAD, UNIT 21\nNORTHVILLE, MB R2M5E5\nTOTAL 10.00'
    alternate='AB&CD\nStore 123\nVisit our website at www.ab-cd.com\nTOTAL 10.00'
    assert ingest.extract_merchant_store(primary,observations=[observation(alternate),observation(alternate)])[0]=='AB&CD'
    assert ingest.extract_merchant_store(primary,observations=[observation(alternate)])[0] is None
    assert ingest.extract_merchant_store(primary,observations=[observation(alternate,page=2),observation(alternate,page=2)])[0] is None
    unrelated=alternate.replace('ab-cd.com','another-shop.com')
    assert ingest.extract_merchant_store(primary,observations=[observation(unrelated),observation(unrelated)])[0] is None
    assert ingest.extract_merchant_store(primary,observations=[observation(alternate,status='failed'),observation(alternate,status='failed')])[0] is None


def test_footer_merchant_alone_is_never_promoted_and_tied_names_remain_unknown():
    primary='1234 Example Road\nVisit our website at www.ab-cd.com'
    assert ingest.merchant_from_observations(primary,[observation(primary),observation(primary)]) is None
    a='AB&CD\nVisit us at www.ab-cd.com'
    b='EF&GH\nVisit us at www.ef-gh.com'
    assert ingest.merchant_from_observations(primary,[observation(a),observation(a),observation(b),observation(b)]) is None


def test_checkout_timestamp_and_transaction_code_are_not_a_line_item_price():
    text='NOISE 09:21AM Reg x Trans..8877\n123456 REG FIT PANTS 40.00\nSubtotal 40.00\nTOTAL 42.00'
    assert [(i.item_name,i.line_total) for i in ingest.extract_items(text)]==[('123456 REG FIT PANTS',40.0)]
    assert ingest.extract_items('09:21AM Register 2 Transaction..8877')==[]


def test_corroborating_website_accepts_protocol_and_www_prefix():
    text='AB&CD\nVisit https://www.ab-cd.com'
    assert ingest.merchant_from_observations('123 Example Road',[observation(text),observation(text)])=='AB&CD'
