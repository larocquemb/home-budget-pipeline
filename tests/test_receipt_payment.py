import unittest

from receipt_payment import extract_payment_provenance, resolve_payer


class ReceiptPaymentTests(unittest.TestCase):
    def test_extracts_visa_last4(self):
        p = extract_payment_provenance("Visa *9809 42.54-")
        self.assertEqual(p.payment_method, "Visa")
        self.assertEqual(p.card_last4, "9809")

    def test_extracts_masked_card_number(self):
        p = extract_payment_provenance("Card Type: CREDIT\nCARD NUMBER: KKKKKKKKKKKK0806 P\nVisa Credit")
        self.assertEqual(p.payment_method, "Visa")
        self.assertEqual(p.card_last4, "0806")

    def test_prefers_debit_terminal_evidence(self):
        p = extract_payment_provenance("ACCT: FLASH DEFAULT CAD$ 23.72\nCard Type: DEBIT\nInterac\nA0000002771010")
        self.assertEqual(p.payment_method, "Debit")
        self.assertIsNone(p.card_last4)

    def test_payer_resolution_is_config_driven(self):
        owners = {"9809": "Paul", "0806": "Roxanne"}
        self.assertEqual(resolve_payer("9809", owners), "Paul")
        self.assertEqual(resolve_payer("0806", owners), "Roxanne")
        self.assertIsNone(resolve_payer("9999", owners))
        self.assertIsNone(resolve_payer(None, owners))


if __name__ == "__main__":
    unittest.main()
