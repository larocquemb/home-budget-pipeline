import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from home_budget_pipeline.receipts import ingest as scan


class ScannedReceiptPaymentIntegrationTests(unittest.TestCase):
    def test_parse_scan_includes_payment_method_and_last4_without_db(self):
        text = """SHOPPERS DRUG MART
2026-04-26
TOTAL 23.72
VISA *9809 23.72
"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "receipt.pdf"
            p.write_bytes(b"fake")
            with patch.object(scan, "extract_page_text", return_value=[text]):
                receipt = scan.parse_scan(p)

        self.assertEqual(receipt.payment_method, "Visa")
        self.assertEqual(receipt.card_last4, "9809")
        self.assertIsNone(receipt.payer)

    def test_receipt_json_contains_payment_provenance(self):
        receipt = scan.ScannedReceipt(
            path="receipt.pdf",
            source_reference="receipt.pdf",
            source_sha256="abc",
            merchant="Sobeys",
            transaction_date="2026-04-26",
            receipt_id=None,
            subtotal=10.0,
            tax=0.5,
            total=10.5,
            payment_method="Mastercard",
            card_last4="1234",
            payer="Paul",
        )
        data = scan.receipt_to_dict(receipt)
        self.assertEqual(data["payment_method"], "Mastercard")
        self.assertEqual(data["card_last4"], "1234")
        self.assertEqual(data["payer"], "Paul")


if __name__ == "__main__":
    unittest.main()
