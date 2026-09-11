import unittest
from unittest.mock import MagicMock

from types import SimpleNamespace

from home_budget_pipeline.receipts.evidence import persist_ocr_lines, persist_ocr_run, record_ocr_feedback


class ReceiptOcrLineTests(unittest.TestCase):
    def test_ocr_run_and_pass_metrics_are_persisted(self):
        conn = MagicMock()
        receipt = SimpleNamespace(
            source_sha256="abc", merchant="Sobeys", extraction_status="complete",
            extraction_confidence=0.95, worker_host="worker-1", worker_pid=42,
            ocr_run={
                "run_uuid": "11111111-1111-1111-1111-111111111111",
                "cache_version": 13, "processed_at": "2026-09-11T00:00:00+00:00",
                "processing_seconds": 1.5, "timings": {"text_extraction_seconds": 1.0},
                "ocr_passes": [{
                    "pass_id": 1, "page_number": 1, "engine": "tesseract",
                    "engine_type": "traditional_ocr", "dpi": 300, "psm": "6",
                    "variant": "raw", "seconds": 0.4, "status": "success",
                    "selected_base": True, "text": "TOTAL $10.00",
                }],
            },
        )

        self.assertEqual(persist_ocr_run(conn, 7, receipt), 1)
        cursor = conn.cursor.return_value.__enter__.return_value
        sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("INSERT INTO budget.receipt_ocr_runs", sql)
        self.assertIn("INSERT INTO budget.receipt_ocr_passes", sql)

    def test_verified_feedback_is_upserted(self):
        conn = MagicMock()

        record_ocr_feedback(conn, 7, "corrected", {"total": "10.00"}, "checked", "paul")

        cursor = conn.cursor.return_value.__enter__.return_value
        query, values = cursor.execute.call_args.args
        self.assertIn("INSERT INTO budget.receipt_ocr_feedback", query)
        self.assertEqual(values[0:2], (7, "corrected"))

    def test_structured_ocr_context_is_persisted(self):
        conn = MagicMock()
        pages = [{
            "page_number": 1,
            "lines": [{
                "line_number": 2,
                "text": "Baguette White $3.99 C",
                "confidence": 94.5,
                "layout": {
                    "bounds": {"x": 140, "y": 250, "width": 500, "height": 35},
                    "page": {"left": 100, "width": 900},
                    "indent": {"pixels": 40, "ratio": 0.0444, "level": 1},
                },
                "line_type": "item",
                "department": "BAKERY",
                "applies_to": None,
            }],
        }]

        self.assertEqual(persist_ocr_lines(conn, 7, pages), 1)
        cursor = conn.cursor.return_value.__enter__.return_value
        insert = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO budget.receipt_ocr_lines" in call.args[0]
        )
        self.assertEqual(insert.args[1][0:5], (7, 1, 2, "Baguette White $3.99 C", 0.945))
        self.assertEqual(insert.args[1][14:16], ("item", "BAKERY"))
