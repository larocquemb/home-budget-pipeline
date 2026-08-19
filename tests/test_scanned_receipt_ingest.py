import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scanned_receipt_ingest as scan


class ScannedReceiptIngestTests(unittest.TestCase):
    def test_discover_scans_recursive_and_supported_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.pdf").write_bytes(b"a")
            (root / "ignore.txt").write_text("x")
            (root / "nested").mkdir()
            (root / "nested" / "b.jpg").write_bytes(b"b")
            self.assertEqual([p.name for p in scan.discover_scans(root)], ["a.pdf", "b.jpg"])

    def test_merge_page_text_removes_long_receipt_overlap(self):
        pages = [
            "SOBEYS\nMilk 5.94\nBread 4.99\nEggs 6.49",
            "Bread 4.99\nEggs 6.49\nApples 8.25\nTOTAL 25.67",
        ]
        merged = scan.merge_page_text(pages)
        self.assertEqual(merged.count("Bread 4.99"), 1)
        self.assertEqual(merged.count("Eggs 6.49"), 1)
        self.assertIn("Apples 8.25", merged)

    def test_parse_common_fields_and_items(self):
        text = """SOBEYS #1234
2026-08-18
TRANSACTION 987654
Milk 5.94
Bread 4.99
SUBTOTAL 10.93
GST 0.55
TOTAL 11.48
"""
        subtotal, tax, total = scan.extract_totals(text)
        self.assertEqual(scan.extract_date(text), "2026-08-18")
        self.assertEqual(scan.extract_receipt_id(text), "987654")
        self.assertEqual(subtotal, 10.93)
        self.assertEqual(tax, 0.55)
        self.assertEqual(total, 11.48)
        self.assertEqual([(x.item_name, x.line_total) for x in scan.extract_items(text)], [("Milk", 5.94), ("Bread", 4.99)])

    def test_total_extraction_accepts_amount_due_and_embedded_total(self):
        self.assertEqual(scan.extract_totals("PURCHASE TOTAL $42.17")[2], 42.17)
        self.assertEqual(scan.extract_totals("AMOUNT DUE 19.84")[2], 19.84)
        self.assertEqual(scan.extract_totals("BALANCE DUE $31.20")[2], 31.20)

    def test_normalize_known_merchant_ocr_variants(self):
        self.assertEqual(scan.normalize_merchant("CANAD TAN TIRE #266"), "Canadian Tire")
        self.assertEqual(scan.normalize_merchant("Tin Hortons # 104156"), "Tim Hortons")
        self.assertEqual(scan.normalize_merchant("im Hortons # 104156"), "Tim Hortons")
        self.assertEqual(scan.normalize_merchant("=SEWHOLESALE"), "Wholesale Club")
        self.assertEqual(scan.normalize_merchant("SHOPPERS &"), "Shoppers Drug Mart")
        self.assertEqual(scan.normalize_merchant("Sobeys Sage Creek"), "Sobeys Sage Creek")

    def test_review_reasons_identify_missing_required_fields(self):
        receipt = scan.ScannedReceipt(
            path="receipt.pdf", source_reference="receipt.pdf", source_sha256="abc",
            merchant="Sobeys", transaction_date=None, receipt_id=None,
            subtotal=None, tax=None, total=None, items=[scan.ScannedItem("Milk", 5.0)],
            text="Sobeys\nMilk 5.00",
        )
        reasons = scan.review_reasons_for(receipt)
        self.assertIn("missing_date", reasons)
        self.assertIn("missing_total", reasons)
        self.assertNotIn("missing_receipt_id", reasons)

    def test_effectively_empty_ocr_is_unreadable(self):
        receipt = scan.ScannedReceipt(
            path="receipt.pdf", source_reference="receipt.pdf", source_sha256="abc",
            merchant=None, transaction_date=None, receipt_id=None,
            subtotal=None, tax=None, total=None, items=[], text="| — _ .",
        )
        receipt.extraction_confidence = scan.confidence_for(receipt)
        self.assertEqual(scan.status_for(receipt), "unreadable")

    def test_content_hash_is_stable_idempotency_key(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "receipt.pdf"
            p.write_bytes(b"same immutable scan")
            h1 = scan.sha256_file(p)
            h2 = scan.sha256_file(p)
            self.assertEqual(h1, h2)
            receipt = scan.ScannedReceipt(
                path=str(p), source_reference=p.name, source_sha256=h1,
                merchant=None, transaction_date=None, receipt_id=None,
                subtotal=None, tax=None, total=None,
            )
            self.assertEqual(receipt.canonical_order_id, f"scan:{h1}")

    def test_parse_scan_routes_incomplete_receipt_to_review(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "receipt.pdf"
            p.write_bytes(b"fake")
            with patch.object(scan, "extract_page_text", return_value=["STORE NAME\n2026-08-18\nTOTAL 12.34"]):
                receipt = scan.parse_scan(p)
            self.assertEqual(receipt.extraction_status, "review")
            self.assertEqual(receipt.total, 12.34)
            self.assertFalse(receipt.items)
            self.assertIn("missing_items", receipt.review_reasons)


if __name__ == "__main__":
    unittest.main()
