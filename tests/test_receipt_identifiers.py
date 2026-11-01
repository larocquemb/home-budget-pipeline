import pytest

from home_budget_pipeline.receipts.ingest import extract_receipt_id


@pytest.mark.parametrize('text,expected', [
    ('TC# 1234 5678 9012 3456 7890', '12345678901234567890'),
    ('tc # 1234-5678-9012', '123456789012'),
    ('TC# 1234 5678\nReceipt # INV98765', 'INV98765'),
    ('TRANS ID 9876543210\nTC# 1234 5678 9012 3456 7890', '12345678901234567890'),
    ('TRANS ID 9876543210\nTC 6B90A35033E627ED', '9876543210'),
    ('TC 6B90A35033E627ED', None),
    ('TC 1234567890123456', None),
    ('TC# 1234 ABCD 5678', None),
    ('TC# 123', None),
    ('TC# 1234 5678 APPROVAL 9999', None),
])
def test_receipt_transaction_code_is_not_a_payment_cryptogram(text, expected):
    assert extract_receipt_id(text) == expected
