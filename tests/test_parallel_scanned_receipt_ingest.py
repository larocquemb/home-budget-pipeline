import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
