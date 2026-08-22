import unittest
from datetime import datetime
from types import SimpleNamespace

from home_budget_pipeline.receipts.evidence import candidate_score, resolve_merchant_alias


class AliasCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql):
        return None

    def fetchall(self):
        return [("michfiels", "Michaels")]


class AliasConnection:
    def cursor(self):
        return AliasCursor()


class ReceiptEvidenceTests(unittest.TestCase):
    def test_database_merchant_alias_resolves_ocr_candidate(self):
        self.assertEqual(
            resolve_merchant_alias(AliasConnection(), "MICHFIELS STORE #3907"),
            "Michaels",
        )
    def receipt(self, **overrides):
        values = dict(
            receipt_id="ABC123",
            total=42.17,
            merchant="Sobeys Sage Creek",
            card_last4="3400",
            transaction_datetime="2026-05-05T18:50:40",
            transaction_date="2026-05-05",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_same_transaction_scores_as_high_confidence(self):
        candidate = {
            "order_id": "ABC123",
            "receipt_id": None,
            "total": 42.17,
            "merchant": "Sobeys Sage Creek",
            "card_last4": "3400",
            "transaction_datetime": "2026-05-05T18:51:10",
            "order_date": "2026-05-05",
        }
        self.assertEqual(candidate_score(self.receipt(), candidate), 1.0)

    def test_psycopg_datetime_candidate_scores_without_type_error(self):
        candidate = {
            "order_id": "ABC123",
            "receipt_id": None,
            "total": 42.17,
            "merchant": "Sobeys Sage Creek",
            "card_last4": "3400",
            "transaction_datetime": datetime(2026, 5, 5, 18, 51, 10),
            "order_date": "2026-05-05",
        }
        self.assertEqual(candidate_score(self.receipt(), candidate), 1.0)

    def test_same_merchant_same_day_different_time_is_not_auto_duplicate(self):
        candidate = {
            "order_id": "OTHER",
            "receipt_id": None,
            "total": 42.17,
            "merchant": "Sobeys Sage Creek",
            "card_last4": "3400",
            "transaction_datetime": "2026-05-05T12:15:00",
            "order_date": "2026-05-05",
        }
        self.assertLess(candidate_score(self.receipt(), candidate), 0.80)

    def test_amount_and_datetime_without_card_still_need_more_evidence(self):
        candidate = {
            "order_id": "OTHER",
            "receipt_id": None,
            "total": 42.17,
            "merchant": "Sobeys Sage Creek",
            "card_last4": None,
            "transaction_datetime": "2026-05-05T18:50:45",
            "order_date": "2026-05-05",
        }
        self.assertLess(candidate_score(self.receipt(), candidate), 0.80)


if __name__ == "__main__":
    unittest.main()
