import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from home_budget_pipeline.receipts import parallel_ingest as parallel


class ParallelScannedReceiptIngestTests(unittest.TestCase):
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
