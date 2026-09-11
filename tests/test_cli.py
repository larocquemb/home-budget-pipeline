import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from home_budget_pipeline import cli


class BrownRookCliTests(unittest.TestCase):
    def test_receipt_feedback_records_ground_truth(self):
        connection = MagicMock()
        with (
            patch.object(cli.scan, "_db_connect", return_value=connection),
            patch.object(cli, "record_ocr_feedback") as record,
            redirect_stdout(io.StringIO()),
        ):
            status = cli.main([
                "receipts", "feedback", "7", "--outcome", "corrected",
                "--corrected-fields", '{"total":"10.00"}', "--verified-by", "paul",
            ])

        self.assertEqual(status, 0)
        record.assert_called_once_with(
            connection, 7, "corrected", {"total": "10.00"}, None, "paul", "budget"
        )
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_database_setup_upgrades_schema(self):
        connection = MagicMock()
        output = io.StringIO()
        result = {
            "base_created": False,
            "enrichment_schema_created": False,
            "receipt_ocr_lines_created": False,
            "receipt_ocr_learning_created": False,
            "receipt_schema_changed": False,
        }
        with (
            patch.object(cli.scan, "_db_connect", return_value=connection) as db_connect,
            patch.object(cli.db_setup, "ensure_database_schema", return_value=result) as ensure,
            redirect_stdout(output),
        ):
            status = cli.main(["database", "setup", "--database-url", "postgresql://test"])

        self.assertEqual(status, 0)
        db_connect.assert_called_once_with("postgresql://test")
        ensure.assert_called_once_with(connection)
        connection.close.assert_called_once_with()
        self.assertIn('"receipt_ocr_lines_schema": "already_current"', output.getvalue())

    def test_database_setup_uses_database_url_environment_default(self):
        connection = MagicMock()
        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://environment/budget"}),
            patch.object(cli.scan, "_db_connect", return_value=connection) as db_connect,
            patch.object(cli.db_setup, "ensure_database_schema", return_value={
                "base_created": False,
                "enrichment_schema_created": False,
                "receipt_ocr_lines_created": False,
                "receipt_ocr_learning_created": False,
                "receipt_schema_changed": False,
            }),
            redirect_stdout(io.StringIO()),
        ):
            status = cli.main(["database", "setup"])

        self.assertEqual(status, 0)
        db_connect.assert_called_once_with("postgresql://environment/budget")
        connection.close.assert_called_once_with()

    def test_ocr_cache_rebuild_uses_environment_paths_without_database(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {
                "RECEIPT_SOURCE_ROOT": "/receipts",
                "HOME_BUDGET_OCR_CACHE": "/cache",
            }),
            patch.object(cli.scan, "discover_scans", return_value=[Path("/receipts/one.pdf")]),
            patch.object(cli, "parse_scans_parallel") as parse_scans,
            patch.object(cli.scan, "_db_connect") as db_connect,
            redirect_stdout(output),
        ):
            result = cli.main(["ocr-cache", "rebuild", "--workers", "3"])

        self.assertEqual(result, 0)
        parse_scans.assert_called_once_with(
            [Path("/receipts/one.pdf")],
            Path("/receipts"),
            3,
            Path("/cache"),
            refresh=True,
        )
        db_connect.assert_not_called()
        self.assertIn('"cache_rebuilt": 1', output.getvalue())

    def test_receipt_process_command_connects_to_database(self):
        connection = MagicMock()
        diagnostic = io.StringIO()
        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://example/budget"}),
            patch.object(cli.scan, "_db_connect", return_value=connection),
            patch.object(cli.socket, "gethostname", return_value="test-host"),
            patch.object(cli.backlog_ingest, "process_backlog", return_value={"failed": 0}) as process,
            redirect_stdout(io.StringIO()),
            redirect_stderr(diagnostic),
        ):
            result = cli.main(["receipts", "process", "/receipts", "--ocr-cache", "/cache", "--verbose"])

        self.assertEqual(result, 0)
        process.assert_called_once()
        self.assertTrue(process.call_args.kwargs["verbose"])
        connection.close.assert_called_once_with()
        settings = diagnostic.getvalue()
        self.assertIn("Receipt root: /receipts", settings)
        self.assertIn("OCR cache: /cache", settings)
        self.assertIn("Workers: 2 local process(es)", settings)
        self.assertIn("Worker host: test-host", settings)
        self.assertIn("Database: postgresql://example/budget", settings)
        self.assertIn("Ingest schema: ingest", settings)
        self.assertIn("Budget schema: budget", settings)
        self.assertIn("Refresh OCR cache: no", settings)

    def test_database_display_redacts_passwords(self):
        self.assertEqual(
            cli._display_database_dsn("postgresql://paul:secret@127.0.0.1:5432/home_budget"),
            "postgresql://paul:***@127.0.0.1:5432/home_budget",
        )
        self.assertEqual(
            cli._display_database_dsn("host=localhost dbname=budget user=paul password=secret"),
            "host=localhost dbname=budget user=paul password=***",
        )
