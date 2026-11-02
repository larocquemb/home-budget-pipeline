import unittest

from home_budget_pipeline.receipts.payment import extract_payment_provenance


class ReceiptPaymentTests(unittest.TestCase):
    def test_extracts_visa_last4(self):
        p = extract_payment_provenance("Visa *9809 42.54-")
        self.assertEqual(p.payment_method, "Visa")
        self.assertEqual(p.card_last4, "9809")

    def test_extracts_masked_card_number(self):
        p = extract_payment_provenance("Card Type: CREDIT\nCARD NUMBER: KKKKKKKKKKKK0806 P\nVisa Credit")
        self.assertEqual(p.payment_method, "Visa")
        self.assertEqual(p.card_last4, "0806")

    def test_brand_credit_label_and_spaced_mask_groups(self):
        for text in ('VISA CREDIT **** **** **** 4321 RF 2',
                     'Mastercard Credit Card xxxx xxxx xxxx 4321',
                     'Amex **** ****** *4321'):
            with self.subTest(text=text):
                self.assertEqual(extract_payment_provenance(text).card_last4, '4321')

    def test_does_not_guess_digits_from_garbled_mask_or_unmasked_account(self):
        for text in ('VISA CREDIT #4 smudged 4321 RF 2',
                     'VISA CREDIT 1234567890124321',
                     'VISA CREDIT **** **** **** 43ZI'):
            with self.subTest(text=text):
                self.assertIsNone(extract_payment_provenance(text).card_last4)

    def test_extracts_semicolon_masked_card_number_from_real_ocr(self):
        p = extract_payment_provenance(
            "CASH SALE\n"
            "ACCT: GIFTCARD $100.00\n"
            "ACCT: MASTERCARD $ 60.33\n"
            "CARD NUMBER; xxxxxxxxxx** 3400\n"
            "Mastercard $60.33"
        )
        self.assertEqual(p.payment_method, "Mastercard")
        self.assertEqual(p.card_last4, "3400")

    def test_cash_sale_does_not_outrank_card_tender(self):
        p = extract_payment_provenance("CASH SALE\nACCT: MASTERCARD $60.33\nMastercard")
        self.assertEqual(p.payment_method, "Mastercard")

    def test_numbered_mask_immediately_below_terminal_card_label(self):
        for label in ('NO.', 'NO,', 'NO:'):
            with self.subTest(label=label):
                p = extract_payment_provenance(
                    f'Master Card TENDER $24.60\nCARD MC RCPT 6281000\n{label} ************7654 RESP 001'
                )
                self.assertEqual(p.card_last4, '7654')

    def test_numbered_mask_requires_adjacent_payment_card_context(self):
        for text in (
            'MEMBER CARD\nNO. ************7654',
            'CARD MC\nMember rewards\nNO. ************7654',
            'CARD MC\nNO. 1234567890127654',
            'CARD MC\nNO. ************76S4',
            'CARD MC\nNO. ************76543',
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_payment_provenance(text).card_last4)

    def test_explicit_cash_tender_is_cash(self):
        p = extract_payment_provenance("CASH TENDER $25.00")
        self.assertEqual(p.payment_method, "Cash")
        self.assertIsNone(p.card_last4)

    def test_prefers_debit_terminal_evidence(self):
        p = extract_payment_provenance("ACCT: FLASH DEFAULT CAD$ 23.72\nCard Type: DEBIT\nInterac\nA0000002771010")
        self.assertEqual(p.payment_method, "Debit")
        self.assertIsNone(p.card_last4)

    def test_garbled_non_numeric_card_tail_is_not_guessed(self):
        p = extract_payment_provenance("CARD NUMBER: KOKI K KK KHKKKK IBY\nVisa Credit")
        self.assertEqual(p.payment_method, "Visa")
        self.assertIsNone(p.card_last4)


if __name__ == "__main__":
    unittest.main()
