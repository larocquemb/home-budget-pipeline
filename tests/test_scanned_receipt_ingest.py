import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from home_budget_pipeline.receipts import ingest as scan


class ScannedReceiptIngestTests(unittest.TestCase):
    def test_ocr_evaluates_all_requested_resolution_and_layout_variants(self):
        self.assertEqual(scan.OCR_RENDER_DPIS, (150, 200, 300))
        self.assertEqual(scan.OCR_PAGE_SEGMENTATION_MODES, ("4", "6", "11"))
        self.assertEqual((scan.OCR_HIGH_DETAIL_DPI, scan.OCR_HIGH_DETAIL_PSM), (450, "6"))

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

    def test_instant_savings_is_not_extracted_as_a_product(self):
        text = """Baguette White $3.99 C
INSTANT SAVINGS $0.50
YOU SAVED $1.30
POINTS EARNED 200 PTS
1.275 kg @ $3.13 / kg
+EHC $0.12"""
        self.assertEqual(
            [(item.item_name, item.line_total) for item in scan.extract_items(text)],
            [("Baguette White", 3.99)],
        )

    def test_item_amount_wrapped_to_next_ocr_line_is_preserved(self):
        text = """079594233699 5PK YARD BAG <A>
203. 44 6.88
SUBTOTAL 6.88
"""
        self.assertEqual(
            [(x.item_name, x.line_total) for x in scan.extract_items(text)],
            [("079594233699 5PK YARD BAG <A>", 6.88)],
        )

    def test_michaels_crct_io_puff_item_description(self):
        self.assertEqual(
            [(item.item_name, item.line_total) for item in scan.extract_items("CRCT IO PUFF 12x1 18.99")],
            [("CRCT PUFF 12x1", 18.99)],
        )

    def test_checkout_upc_quantity_and_unit_price_are_not_part_of_item_name(self):
        text = """CRCT IO PUFF 12x1 92573913341 1 @ 18.99-
CRCT IO PUFF 12x1 93573447143 1 @ 18.98-
CRCT IO PUFF 12x1 93573447143 1 @ .01-"""
        self.assertEqual(
            [(item.item_name, item.line_total) for item in scan.extract_items(text)],
            [
                ("CRCT PUFF 12x1", -18.99),
                ("CRCT PUFF 12x1", -18.98),
                ("CRCT PUFF 12x1", -0.01),
            ],
        )

    def test_damaged_michaels_cricut_rows_share_a_stable_product_name(self):
        names = [
            "CRCT 1] PUFF 12x1 3 3913341 1 @",
            "EReT 12 PUFF 12x! 93673447143 1 @",
            "CRCT 1] PUFF 12x) Gah 73447143 1 @",
        ]
        self.assertEqual(
            [scan.clean_extracted_item_name(name) for name in names],
            ["CRCT PUFF 12x1"] * 3,
        )

    def test_item_amount_accepts_contextual_missing_leading_zero(self):
        self.assertEqual(
            [(item.item_name, item.line_total) for item in scan.extract_items("CRCT IO PUFF 12x1 @ .01-")],
            [("CRCT PUFF 12x1", -0.01)],
        )
        self.assertEqual(scan.normalize_leading_decimal_money("Visa *9809 42 .54-"), "Visa *9809 42 .54-")

    def test_date_extraction_accepts_real_ocr_formats(self):
        self.assertEqual(scan.extract_date("DATE/TIME: 26/04/26 19:01:05"), "2026-04-26")
        self.assertEqual(scan.extract_date("13-May-2026 17:39:52"), "2026-05-13")
        self.assertEqual(scan.extract_date("226001555720260707"), "2026-07-07")

    def test_literal_timestamp_quality_rejects_impossible_ocr_date(self):
        self.assertFalse(scan._has_literal_valid_timestamp("SALE 6/31,'26 10:07"))
        self.assertTrue(scan._has_literal_valid_timestamp("SALE 5/31/26 10:07"))

    def test_embedded_pdf_text_rejects_flattened_tesseract_tsv(self):
        flattened_tsv = (
            "5 1 3 1 1 1 581 173 174 67 39.970039 Center "
            "5 1 3 1 1 2 779 170 128 42 92.893394 Roast "
            "5 1 3 1 1 3 932 168 130 43 28.784584 Bniss"
        )
        self.assertTrue(scan._looks_like_tesseract_tsv(flattened_tsv))
        self.assertEqual(scan._usable_embedded_text(flattened_tsv), "")
        self.assertEqual(scan._usable_embedded_text("Center Roast $11.00"), "Center Roast $11.00")

    def test_ocr_candidate_scoring_prefers_recognizable_receipt_fields(self):
        sparse_noise = "STORE 123 random text"
        receipt = "TRANSACTION 123\n5/31/26 10:07\nSUBTOTAL 10.00\nGST 0.50\nTOTAL 10.50"
        self.assertGreater(scan._ocr_candidate_score(receipt), scan._ocr_candidate_score(sparse_noise))

    def test_ocr_candidate_tie_prefers_later_high_detail_pass(self):
        low_detail = "Pretzel Cracker Rane $5.49"
        high_detail = "Pretzel Cracker Ranc $5.49"
        self.assertEqual(scan._ocr_candidate_score(low_detail), scan._ocr_candidate_score(high_detail))
        candidates = [
            scan.OCRCandidate(low_detail, (scan.OCRLine(low_detail, 95.0),), 300, "6"),
            scan.OCRCandidate(high_detail, (scan.OCRLine(high_detail, 91.7),), 450, "6"),
        ]
        self.assertEqual(scan._select_ocr_candidate(candidates).text, high_detail)
        self.assertEqual(scan._line_consensus_text(candidates), high_detail)

    def test_line_consensus_prefers_valid_timestamp_over_impossible_date(self):
        invalid = "SALE 6/31/26 10:07"
        valid = "SALE 5/31/26 10:07"
        candidates = [
            scan.OCRCandidate(invalid, (scan.OCRLine(invalid, 99.0),), 450, "6"),
            scan.OCRCandidate(valid, (scan.OCRLine(valid, 80.0),), 200, "6"),
        ]
        self.assertEqual(scan._line_consensus_text(candidates), valid)

    def test_paddle_confidence_improves_descriptions_without_losing_amounts(self):
        tesseract_lines = (
            scan.OCRLine("Cep Pic Med $6.49 C", 89.0),
            scan.OCRLine("Cep Pic Med $6.49 C", 89.0),
            scan.OCRLine("Pretzel Cracker Rane $5.49 BC", 95.0),
        )
        paddle_lines = (
            scan.OCRLine("Oep Pic Med", 99.13),
            scan.OCRLine("Cep Pic Med", 98.37),
            scan.OCRLine("Pretzel Cracker Ranc", 99.91),
        )
        candidates = [
            scan.OCRCandidate("\n".join(line.text for line in tesseract_lines), tesseract_lines, 450, "6"),
            scan.OCRCandidate(
                "\n".join(line.text for line in paddle_lines),
                paddle_lines,
                200,
                "paddle",
                "paddle",
            ),
        ]
        self.assertEqual(
            scan._line_consensus_text(candidates).splitlines(),
            [
                "Oep Pic Med $6.49 C",
                "Oep Pic Med $6.49 C",
                "Pretzel Cracker Ranc $5.49 BC",
            ],
        )

    def test_ocr_pass_metric_uses_common_versioned_schema(self):
        candidate = scan.OCRCandidate(
            "TOTAL $12.34",
            (scan.OCRLine("TOTAL $12.34", 98.0),),
            200,
            "paddle",
            "paddle",
        )

        metric = scan._ocr_pass_metric(candidate, page=1, variant="raw", seconds=1.25)
        self.assertEqual(metric["schema_version"], 1)
        self.assertEqual(metric["engine"], "paddle")
        self.assertEqual(metric["engine_type"], "traditional_ocr")
        self.assertEqual(metric["status"], "success")
        self.assertEqual(metric["quality"]["line_count"], 1)
        self.assertEqual(metric["quality"]["summary_score"], metric["summary_score"])
        self.assertEqual(metric["engine_options"]["recognition_model"], "PP-OCRv6_medium_rec")
        self.assertEqual(metric["usage"], {})
        self.assertEqual(metric["provenance"], {})

    def test_failed_ocr_pass_records_failure_status(self):
        candidate = scan.OCRCandidate(
            "", (), 200, "paddle", "paddle", "failed", "RuntimeError"
        )

        metric = scan._ocr_pass_metric(candidate, page=1, variant="raw", seconds=0.5)

        self.assertEqual(metric["status"], "failed")
        self.assertEqual(metric["error_type"], "RuntimeError")

    def test_alternate_ocr_supplements_only_missing_summary_fields(self):
        primary = "MICHAELS\n5/31/26 10:07\nITEM 37.98-\nSUBTOTAL 37.98-"
        alternative = "ITEM 37.98-\nGST 1.90-\nRST 2.66-\nTOTFL 42.54-"
        combined = scan._supplement_missing_receipt_summary(primary, alternative)
        self.assertEqual(scan.extract_totals(combined), (-37.98, -4.56, -42.54))
        self.assertEqual(combined.count("ITEM 37.98-"), 1)

    def test_alternate_ocr_supplements_payment_details(self):
        combined = scan._supplement_missing_receipt_summary(
            "MICHAELS\nSUBTOTAL 37.98-",
            "Visa *9809 42 .54-",
        )
        payment = scan.extract_payment_provenance(combined)
        self.assertEqual((payment.payment_method, payment.card_last4), ("Visa", "9809"))
        self.assertEqual(scan.extract_totals(combined)[2], -42.54)

    def test_total_and_subtotal_recover_missing_combined_tax(self):
        self.assertEqual(
            scan.extract_totals("SUBTOTAL 37.98-\nGST 1,90-\nVisa *9809 42.54-"),
            (-37.98, -4.56, -42.54),
        )

    def test_refund_total_restores_item_minus_signs_lost_by_ocr(self):
        items = [scan.ScannedItem("Returned item", 18.99), scan.ScannedItem("Returned item 2", -18.98)]
        self.assertEqual(
            [item.line_total for item in scan.normalize_item_signs(items, -42.54)],
            [-18.99, -18.98],
        )

    def test_ocr_preprocessing_crops_scanner_whitespace(self):
        from PIL import Image, ImageDraw

        image = Image.new("L", (500, 700), 255)
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 100, 350, 500), fill=245)
        for y in range(150, 451, 20):
            draw.line((175, y, 325, y), fill=20, width=3)
        prepared = scan._prepare_ocr_image(image)
        self.assertLess(prepared.width, image.width)

    def test_total_extraction_accepts_amount_due_and_tender_fallback(self):
        self.assertEqual(scan.extract_totals("PURCHASE TOTAL $42.17")[2], 42.17)
        self.assertEqual(scan.extract_totals("AMOUNT DUE 19.84")[2], 19.84)
        self.assertEqual(scan.extract_totals("BALANCE DUE $31.20")[2], 31.20)
        self.assertEqual(scan.extract_totals("ACCT: VISA CAD$ 56.39")[2], 56.39)
        self.assertEqual(scan.extract_totals("VISA 95.89")[2], 95.89)
        self.assertEqual(scan.extract_totals("Mastercard $60.33\nCHANGE $0.06")[2], 60.33)
        self.assertEqual(scan.extract_totals("Trans Type:Purchase $51.37")[2], 51.37)
        self.assertEqual(scan.extract_totals("AMOUNT $22.59\nAPPROVED")[2], 22.59)
        self.assertEqual(scan.extract_totals("Visa *9809 42.54- ‘")[2], -42.54)
        self.assertEqual(scan.extract_totals("TOT AL 40.00")[2], 40.00)
        self.assertEqual(scan.extract_totals("JTAL 66.85")[2], 66.85)

    def test_total_extraction_tolerates_anchored_ocr_punctuation_damage(self):
        self.assertEqual(scan.extract_totals("JTAL 66:85\nVisa ¥9809 66. 85")[2], 66.85)
        self.assertEqual(scan.extract_totals("ACCT: MASTERCARD $ 12:13")[2], 12.13)
        self.assertEqual(scan.extract_totals("MOUNT: $33,.59-\nMasterCard 33,59-")[2], -33.59)
        self.assertEqual(scan.extract_totals("TOTAE P2229 .02\nMasterCard TENDER $229 .02")[2], 229.02)
        self.assertEqual(scan.extract_totals("Toto MESA CAD$ 62.62")[2], 62.62)
        self.assertEqual(scan.extract_totals("Visa *9809 42 .54- ‘")[2], -42.54)

    def test_normalize_known_merchant_ocr_variants(self):
        self.assertEqual(scan.normalize_merchant("CANAD TAN TIRE #266"), "Canadian Tire")
        self.assertEqual(scan.normalize_merchant("Tin Hortons # 104156"), "Tim Hortons")
        self.assertEqual(scan.normalize_merchant("im Hortons # 104156"), "Tim Hortons")
        self.assertEqual(scan.normalize_merchant("=SEWHOLESALE"), "Wholesale Club")
        self.assertEqual(scan.normalize_merchant("SHOPPERS &"), "Shoppers Drug Mart")
        self.assertEqual(scan.normalize_merchant("Sobeys Sage Creek"), "Sobeys Sage Creek")

    def test_known_merchant_wins_over_slogan_in_receipt_header(self):
        text = """Everything to create anything
MICHFIELS STORE #3907
SIGN-UP AT MICHAELS.CA
THANK YOU FOR SHOPPING AT MICHAELS
Policies are available at Michaels.ca"""
        self.assertEqual(scan.extract_merchant(text), "MICHAELS")

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
