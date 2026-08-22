import unittest

from home_budget_pipeline.receipts.datetime import date_part, extract_transaction_datetime


class ReceiptDatetimeTests(unittest.TestCase):
    def test_month_name_with_time(self):
        self.assertEqual(
            extract_transaction_datetime("May 18, 2026 2:32 PM"),
            "2026-05-18T14:32:00",
        )

    def test_month_name_invoice_date(self):
        self.assertEqual(
            extract_transaction_datetime("INVOICE: 3000967594 May 10, 2026"),
            "2026-05-10",
        )

    def test_numeric_mdy_with_time(self):
        self.assertEqual(
            extract_transaction_datetime("07/09/26 11:25:23"),
            "2026-07-09T11:25:23",
        )

    def test_terminal_yymmdd_with_time(self):
        self.assertEqual(
            extract_transaction_datetime("PIES TIME : 26/06/03 17:30:42"),
            "2026-06-03T17:30:42",
        )

    def test_noisy_terminal_yymmdd(self):
        self.assertEqual(
            extract_transaction_datetime("DATE/TIME: 26/05,'31 10:35:11"),
            "2026-05-31T10:35:11",
        )

    def test_repairs_michaels_five_misread_as_six_and_ignores_policy_date(self):
        text = """4171968 SALE RIN 925! 3907 040 6/31,'26 10:07
Effective 11/27/2022 Clearance sales are considered final
6/31/26 10:07"""
        self.assertEqual(
            extract_transaction_datetime(text),
            "2026-05-31T10:07:00",
        )

    def test_prefers_later_receipt_timestamp_over_effective_policy_date(self):
        text = """SALE £/31/26 10:07
Effective 11/27/2022 Clearance sales are considered final
5/31/26 10:07"""
        self.assertEqual(
            extract_transaction_datetime(text),
            "2026-05-31T10:07:00",
        )

    def test_date_part(self):
        self.assertEqual(date_part("2026-05-18T14:32:00"), "2026-05-18")
        self.assertEqual(date_part("2026-05-18"), "2026-05-18")
        self.assertIsNone(date_part(None))


if __name__ == "__main__":
    unittest.main()
