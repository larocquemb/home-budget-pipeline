import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from home_budget_pipeline.receipts import parallel_ingest as parallel


class ParallelScannedReceiptIngestTests(unittest.TestCase):
    def test_ocr_pass_keys_use_one_run_uuid_and_serial_pass_ids(self):
        passes = [{"engine": "tesseract"}, {"engine": "paddle"}]

        run_uuid = parallel._assign_ocr_pass_keys(passes)

        self.assertEqual(str(uuid.UUID(run_uuid)), run_uuid)
        self.assertEqual([item["pass_id"] for item in passes], [1, 2])
        self.assertEqual({item["run_uuid"] for item in passes}, {run_uuid})

    def test_ocr_passes_put_selected_base_first_for_each_page(self):
        passes = [
            {"pass_id": "p1-low", "page_number": 1, "selected_base": False,
             "consensus_coverage_ratio": 0.5, "structural_score": 20},
            {"pass_id": "p2-other", "page_number": 2, "selected_base": False,
             "consensus_coverage_ratio": 0.9, "structural_score": 90},
            {"pass_id": "p1-selected", "page_number": 1, "selected_base": True,
             "consensus_coverage_ratio": 0.7, "structural_score": 50},
            {"pass_id": "p1-high", "page_number": 1, "selected_base": False,
             "consensus_coverage_ratio": 0.8, "structural_score": 40},
            {"pass_id": "p2-selected", "page_number": 2, "selected_base": True,
             "consensus_coverage_ratio": 0.6, "structural_score": 60},
        ]

        parallel._order_ocr_passes(passes)

        self.assertEqual(
            [item["pass_id"] for item in passes],
            ["p1-selected", "p1-high", "p1-low", "p2-selected", "p2-other"],
        )

    def test_ocr_cache_groups_line_arrays_by_source_page(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            path = cache_dir / "receipt.pdf"
            path.write_bytes(b"receipt")

            with patch.object(
                parallel.scan,
                "extract_page_text",
                return_value=["first\nsecond", "third"],
            ):
                pages, cache_hit = parallel._read_or_create_pages(
                    path, "abc123", cache_dir, False, "sobeys/receipt.pdf"
                )

            cache_path = cache_dir / "sobeys" / "receipt.pdf.json"
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertFalse(cache_hit)
            self.assertEqual(pages, ["first\nsecond", "third"])
            metadata = payload["metadata"]
            self.assertEqual(metadata["schema_version"], 1)
            self.assertEqual(metadata["cache_version"], 13)
            geometry = metadata["ocr_passes"][-1]
            self.assertEqual(geometry["variant"], "geometry")
            self.assertEqual(geometry["pass_id"], 1)
            self.assertEqual(geometry["run_uuid"], metadata["run_uuid"])
            self.assertEqual(geometry["schema_version"], 1)
            self.assertEqual(geometry["engine_type"], "layout_ocr")
            self.assertEqual(geometry["status"], "success")
            self.assertEqual(geometry["quality"]["line_count"], 3)
            self.assertEqual(geometry["engine_options"], {"psm": "6"})
            self.assertNotIn("source_filename", payload)
            self.assertEqual(metadata["source_reference"], "sobeys/receipt.pdf")
            self.assertEqual(metadata["source_sha256"], "abc123")
            self.assertIn("+00:00", metadata["processed_at"])
            self.assertGreaterEqual(metadata["processing_seconds"], 0)
            self.assertEqual(
                set(metadata["timings"]),
                {
                    "text_extraction_seconds",
                    "layout_extraction_seconds",
                    "structuring_seconds",
                },
            )
            self.assertTrue(all(value >= 0 for value in metadata["timings"].values()))
            self.assertEqual(payload["plain_text"], "first\nsecond\nthird")
            self.assertEqual(
                [[line["text"] for line in page["lines"]] for page in payload["pages"]],
                [["first", "second"], ["third"]],
            )
            self.assertIn('"text": "first"', cache_path.read_text(encoding="utf-8"))
            cached_pages, second_hit = parallel._read_or_create_pages(
                path, "abc123", cache_dir, False, "sobeys/receipt.pdf"
            )
            self.assertTrue(second_hit)
            self.assertEqual(cached_pages, pages)
            self.assertEqual(parallel._ocr_run_metadata(cache_path)["run_uuid"], metadata["run_uuid"])

    def test_cache_path_preserves_source_filename_and_extension(self):
        self.assertEqual(
            parallel._cache_path(Path("/cache"), "2026-02-14/receipt.pdf", "abc123"),
            Path("/cache/2026-02-14/receipt.pdf.json"),
        )

    def test_version_one_ocr_cache_is_flattened_when_read(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "abc123.json").write_text(
                json.dumps({
                    "cache_version": 1,
                    "source_sha256": "abc123",
                    "page_text": ["first\nsecond", "third"],
                }),
                encoding="utf-8",
            )

            pages, cache_hit = parallel._read_or_create_pages(
                cache_dir / "receipt.pdf", "abc123", cache_dir, False
            )

            self.assertTrue(cache_hit)
            self.assertEqual(pages, ["first\nsecond", "third"])

    def test_version_two_flat_cache_remains_readable_as_one_page(self):
        data = {
            "cache_version": 2,
            "source_sha256": "abc123",
            "page_text": ["first", "second", "third"],
        }

        self.assertEqual(parallel._cached_pages(data), ["first\nsecond\nthird"])

    def test_ocr_cache_discards_flattened_tesseract_metadata(self):
        metadata = " ".join([
            "5 1 3 1 1 1 581 173 174 67 39.970039 Center",
            "5 1 3 1 1 2 779 170 128 42 92.893394 Roast",
            "5 1 3 1 1 3 932 168 130 43 28.784584 Bniss",
        ])

        self.assertEqual(
            parallel._page_text_lines(f"BAKERY\n{metadata}\nTOTAL $11.00"),
            ["BAKERY", "TOTAL $11.00"],
        )

    def test_cache_layout_preserves_geometry_and_department_context(self):
        pages = parallel._cache_layout_pages([[
            parallel.scan.OCRLine("BAKERY", 98.0, 100, 200, 180, 40),
            parallel.scan.OCRLine("Baguette White $3.99 C", 94.5, 140, 250, 500, 35),
            parallel.scan.OCRLine("INSTANT SAVINGS $0.50", 93.0, 140, 290, 400, 35),
            parallel.scan.OCRLine("POINTS EARNED 200 PTS", 92.0, 140, 330, 400, 35),
            parallel.scan.OCRLine("1.275 kg @ $3.13 / kg", 95.0, 140, 370, 400, 35),
            parallel.scan.OCRLine("+EHC $0.12", 96.0, 140, 410, 200, 35),
            parallel.scan.OCRLine("SUBTOTAL $3.99", 97.0, 300, 900, 340, 35),
        ]])

        heading, subtotal = pages[0]["lines"]
        item = heading["children"][0]
        discount, points, price_detail, fee = item["children"]
        self.assertEqual(heading["line_type"], "department_heading")
        self.assertEqual(item["department"], "BAKERY")
        self.assertEqual(
            item["layout"]["bounds"],
            {"x": 140, "y": 250, "width": 500, "height": 35},
        )
        self.assertEqual(item["layout"]["page"]["left"], 100)
        self.assertEqual(item["layout"]["indent"]["pixels"], 40)
        self.assertEqual(item["layout"]["indent"]["level"], 1)
        self.assertGreater(item["layout"]["indent"]["ratio"], 0)
        self.assertEqual(discount["line_type"], "discount")
        self.assertNotIn("applies_to", discount)
        self.assertEqual(points["line_type"], "points")
        self.assertEqual(price_detail["line_type"], "price_detail")
        self.assertEqual(fee["line_type"], "fee")
        for detail in (points, price_detail, fee):
            self.assertNotIn("applies_to", detail)
        self.assertIsNone(subtotal["department"])

    def test_cache_json_keeps_bounds_on_one_physical_line(self):
        rendered = parallel._format_cache_json({
            "pages": [{
                "lines": [{
                    "text": "Baguette White $3.99 C",
                    "layout": {
                        "bounds": {"x": 140, "y": 160, "width": 500, "height": 35},
                        "page": {"left": 100, "width": 900},
                        "indent": {"pixels": 40, "ratio": 0.0444, "level": 1},
                    },
                }],
            }],
        })

        self.assertIn('"bounds": {"x": 140, "y": 160, "width": 500, "height": 35}', rendered)
        self.assertIn('"page": {"left": 100, "width": 900}', rendered)
        self.assertIn('"indent": {"pixels": 40, "ratio": 0.0444, "level": 1}', rendered)
        self.assertEqual(json.loads(rendered)["pages"][0]["lines"][0]["layout"]["bounds"]["x"], 140)

    def test_parallel_parser_preserves_input_order(self):
        paths = [Path("c.pdf"), Path("a.pdf"), Path("b.pdf")]

        def fake_parse(path, root):
            return str(path)

        with patch.object(parallel.scan, "parse_scan", side_effect=fake_parse):
            result = parallel.parse_scans_parallel(paths, Path("."), workers=3)

        self.assertEqual(result, ["c.pdf", "a.pdf", "b.pdf"])

    def test_workers_are_clamped_to_one(self):
        paths = [Path("a.pdf")]
        with patch.object(parallel.scan, "parse_scan", return_value="a.pdf"):
            result = parallel.parse_scans_parallel(paths, Path("."), workers=0)
        self.assertEqual(result, ["a.pdf"])

    def test_refresh_replaces_items_for_existing_receipt_evidence(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (42,)
        receipt = SimpleNamespace(merchant="Sobeys")

        with (
            patch.object(parallel, "resolve_merchant_alias", return_value="Sobeys"),
            patch.object(parallel, "upsert_evidence", return_value=7),
            patch.object(parallel.scan, "upsert_receipt", return_value=42) as upsert_receipt,
            patch.object(parallel, "_persist_transaction_datetime"),
            patch.object(parallel, "attach_evidence") as attach_evidence,
            patch.object(parallel, "find_match") as find_match,
        ):
            stats = parallel.persist_evidence_first(
                conn,
                [receipt],
                "budget",
                replace_existing=True,
            )

        upsert_receipt.assert_called_once_with(conn, receipt, "budget")
        attach_evidence.assert_called_once_with(conn, 7, 42, "budget")
        find_match.assert_not_called()
        self.assertEqual(stats, {"matched": 1, "ambiguous": 0, "new": 0})


if __name__ == "__main__":
    unittest.main()
