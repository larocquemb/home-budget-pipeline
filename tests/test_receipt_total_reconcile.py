import unittest

from home_budget_pipeline.receipts.total_reconcile import reconcile_total_from_text


class ReceiptTotalReconcileTests(unittest.TestCase):
    def test_recovers_single_subtotal_and_tax_when_total_is_damaged(self):
        text = """SUBTOT $ 120.49
13% HST $ 15.66
TOT AE NOME 1396.15
VISA*TEND =F 136415
"""
        self.assertEqual(reconcile_total_from_text(text, None), 136.15)

    def test_recovers_percentage_only_tax_ocr_line(self):
        text = """SUBTOT $ 120.49
13% oo $ 15.66
TOT AE NOME 1396.15
VISA*TEND =F 136415
"""
        self.assertEqual(reconcile_total_from_text(text, None), 136.15)

    def test_keeps_existing_total(self):
        self.assertEqual(reconcile_total_from_text("SUBTOTAL 10.00\nTAX 1.30\nTOTAL 11.30", 11.30), 11.30)

    def test_refuses_multiple_subtotal_sections(self):
        text = """SUBTOTAL 211.91
TAX 32.63
TOTAL unreadable
SUBTOTAL 684.64
TAX 38.29
TOTAL unreadable
"""
        self.assertIsNone(reconcile_total_from_text(text, None))

    def test_refuses_multiple_tax_sections(self):
        text = """SUBTOTAL 304.50
TAX 3.93
TOTAL unreadable
SUBTOTAL 11.79
TAX unreadable
"""
        self.assertIsNone(reconcile_total_from_text(text, None))


if __name__ == "__main__":
    unittest.main()
