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
