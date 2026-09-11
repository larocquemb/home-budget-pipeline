import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from home_budget_pipeline.receipts import parallel_ingest as parallel


class ReceiptMetadataSchemaTests(unittest.TestCase):
    def _run_ocr(self, path: Path, cache_dir: Path, *, refresh: bool, source_sha256: str = "abc123"):
        layout = [[
            parallel.scan.OCRLine("Sobeys", 99.0, 100, 100, 200, 30),
            parallel.scan.OCRLine("TOTAL $10.00", 99.0, 100, 200, 250, 30),
        ]]
        with (
            patch.object(parallel.scan, "extract_page_text", return_value=["Sobeys\nTOTAL $10.00"]),
            patch.object(parallel.scan, "extract_page_layout", return_value=layout),
        ):
            return parallel._read_or_create_pages(
                path,
                source_sha256,
                cache_dir,
                refresh,
                "2026-09-11/receipt.pdf",
            )

    def test_current_metadata_schema_has_required_traceability_fields(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            path = cache_dir / "receipt.pdf"
            path.write_bytes(b"receipt")

            pages, cache_hit = self._run_ocr(path, cache_dir, refresh=False)

            self.assertFalse(cache_hit)
            self.assertEqual(pages, ["Sobeys\nTOTAL $10.00"])
            payload = json.loads(
                (cache_dir / "2026-09-11" / "receipt.pdf.json").read_text(encoding="utf-8")
            )
            metadata = payload["metadata"]

            self.assertEqual(
                set(metadata),
                {
                    "schema_version",
                    "cache_version",
                    "run_uuid",
                    "source_reference",
                    "source_sha256",
                    "processed_at",
                    "processing_seconds",
                    "timings",
                    "ocr_passes",
                },
            )
            self.assertEqual(metadata["schema_version"], 1)
            self.assertEqual(metadata["cache_version"], 13)
            self.assertEqual(metadata["source_reference"], "2026-09-11/receipt.pdf")
            self.assertEqual(metadata["source_sha256"], "abc123")
            self.assertGreaterEqual(metadata["processing_seconds"], 0)
            self.assertTrue(metadata["ocr_passes"])

            processed_at = datetime.fromisoformat(metadata["processed_at"])
            self.assertIsNotNone(processed_at.tzinfo)
            self.assertEqual(processed_at.utcoffset().total_seconds(), 0)

    def test_refresh_replaces_existing_cache_and_creates_new_run_identity(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            path = cache_dir / "receipt.pdf"
            path.write_bytes(b"receipt")

            self._run_ocr(path, cache_dir, refresh=False)
            cache_path = cache_dir / "2026-09-11" / "receipt.pdf.json"
            first_payload = json.loads(cache_path.read_text(encoding="utf-8"))

            pages, cache_hit = self._run_ocr(path, cache_dir, refresh=True)
            second_payload = json.loads(cache_path.read_text(encoding="utf-8"))

            self.assertFalse(cache_hit)
            self.assertEqual(pages, ["Sobeys\nTOTAL $10.00"])
            self.assertNotEqual(
                first_payload["metadata"]["run_uuid"],
                second_payload["metadata"]["run_uuid"],
            )
            self.assertEqual(second_payload["metadata"]["source_sha256"], "abc123")
            self.assertEqual(
                second_payload["metadata"]["source_reference"],
                "2026-09-11/receipt.pdf",
            )

    def test_cache_with_wrong_source_hash_is_regenerated(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            path = cache_dir / "receipt.pdf"
            path.write_bytes(b"receipt")
            cache_path = cache_dir / "2026-09-11" / "receipt.pdf.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "schema_version": 1,
                            "cache_version": 13,
                            "source_reference": "2026-09-11/receipt.pdf",
                            "source_sha256": "stale-hash",
                        },
                        "plain_text": "STALE",
                        "pages": [
                            {
                                "page_number": 1,
                                "lines": [{"text": "STALE"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            pages, cache_hit = self._run_ocr(path, cache_dir, refresh=False)
            payload = json.loads(cache_path.read_text(encoding="utf-8"))

            self.assertFalse(cache_hit)
            self.assertEqual(pages, ["Sobeys\nTOTAL $10.00"])
            self.assertEqual(payload["metadata"]["source_sha256"], "abc123")
            self.assertNotEqual(payload["plain_text"], "STALE")

    def test_metadata_reader_remains_backward_compatible_with_legacy_cache(self):
        legacy = {
            "cache_version": 2,
            "source_sha256": "abc123",
            "page_text": ["first", "second"],
        }
        current = {
            "metadata": {
                "schema_version": 1,
                "cache_version": 13,
                "source_sha256": "abc123",
            }
        }

        self.assertIs(parallel._cache_metadata(legacy), legacy)
        self.assertIs(parallel._cache_metadata(current), current["metadata"])
        self.assertEqual(parallel._cached_pages(legacy), ["first\nsecond"])


if __name__ == "__main__":
    unittest.main()
