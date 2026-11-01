import pytest

from home_budget_pipeline.receipts.datetime import extract_transaction_datetime, allows_filename_date
from home_budget_pipeline.receipts import ingest, parallel_ingest


@pytest.mark.parametrize('text,expected', [
    ('Printed Date: 12/04/2028 Work Completed: 03/18/2025', '2025-03-18'),
    ('Work Completed: 03/18/2025 Printed Date: 12/04/2028', '2025-03-18'),
    ('Printed Date: December 4, 2028 2:32 PM\nService Date: 2025-03-18', '2025-03-18'),
    ('Invoice Date: 2024-09-12\nReprinted On: 11/09/2027', '2024-09-12'),
    ('Date Printed: 2028-03-04\nPurchase Date: 09/12/2024 14:15:16', '2024-09-12T14:15:16'),
    ('Order Date: February 3 2025\nShipped On: 02/07/2025', '2025-02-03'),
    ('Printed Date:\n12/04/2028\nWork Completed:\n03/18/2025', '2025-03-18'),
    ('PrintedDate: 2028-12-04\nInvoice Date: 18-Mar-2025', '2025-03-18'),
    ('Printed: 2028-12-04\nDate of Purchase: 2025-03-18', '2025-03-18'),
    ('Printed Date: 2028-12-04', None),
    ('Printed 2028-12-04', None),
    ('Reprinted:\nDecember 4, 2028', None),
    ('Payment Due Date: 2028-12-04', None),
    ('Vehicle Received: 2028-12-04', None),
    ('Date Printed: 06/02/2028\nDate Promised: 05/19/2028\nCashier Date: 06/02/2028', '2028-06-02'),
    ('Date Printed: 06/02/2028\nDate Promised: 05/19/2028', None),
    ('Invoice Date: 2025-03-18\nPaid On: 2025-04-21\nPrinted Date: 2028-12-04', '2025-03-18'),
    ('Transaction Date: 2025-03-18\nTransaction Date: 2025-03-19', None),
    ('Effective December 4, 2028\n2025-03-18 09:24', '2025-03-18T09:24:00'),
    ('Warranty expires 2028-12-04\n2025-03-18 09:24', '2025-03-18T09:24:00'),
    ('PRINTED T-SHIRT 12.34\n03/18/2025 09:24', '2025-03-18T09:24:00'),
])
def test_date_purpose_is_independent_of_order_format_and_merchant(text, expected):
    assert extract_transaction_datetime(text) == expected
    assert ingest.extract_date(text) == (expected[:10] if expected else None)


def test_printed_product_is_not_a_print_date_label():
    assert allows_filename_date('PRINTED T-SHIRT 12.34')


@pytest.mark.parametrize('text,expected', [
    ('EXAMPLE REPAIR\nPrinted Date: 12/04/2028 Work Completed: 03/18/2025\nTOTAL 10.00', '2025-03-18'),
    ('EXAMPLE SHOP\nPrinted Date: 12/04/2028\nTOTAL 10.00', None),
    ('EXAMPLE SHOP\nPurchase Date: 03/18/2025\nPurchase Date: 03/19/2025\nTOTAL 10.00', None),
])
def test_cached_and_direct_import_share_purpose_rule_not_filename_guess(tmp_path, monkeypatch, text, expected):
    path = tmp_path / 'document.pdf'
    path.write_bytes(b'synthetic source')
    monkeypatch.setattr(ingest, 'extract_page_text', lambda *a: [text])
    monkeypatch.setattr(ingest, 'receipt_info_from_filename', lambda *a: ('2030-12-01', 'Example', 10.0))
    monkeypatch.setattr(parallel_ingest, '_read_or_create_pages', lambda *a: ([text], True))
    direct = ingest.parse_scan(path, tmp_path)
    cached, hit = parallel_ingest._parse_scan_cached(path, tmp_path, tmp_path/'cache', False)
    assert direct.transaction_date == cached.transaction_date == expected
    assert hit
