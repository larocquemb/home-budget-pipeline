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

    def test_database_display_redacts_passwords(self):
        self.assertEqual(
            cli._display_database_dsn("postgresql://paul:secret@127.0.0.1:5432/home_budget"),
            "postgresql://paul:***@127.0.0.1:5432/home_budget",
        )
        self.assertEqual(
            cli._display_database_dsn("host=localhost dbname=budget user=paul password=secret"),
            "host=localhost dbname=budget user=paul password=***",
        )
