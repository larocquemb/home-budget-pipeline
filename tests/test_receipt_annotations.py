import unittest

from receipt_annotations import extract_receipt_annotations


class ReceiptAnnotationTests(unittest.TestCase):
    def test_repeated_subtotals_become_category_annotations(self):
        text = """ITEM A 10.00
SUBTOTAL 211.91
TAX 32.63
Chloe owes 15 A
ITEM B 20.00
SUBTOTAL 684.64
TAX 38.29
TOTAL unreadable
"""
        annotations = extract_receipt_annotations(text)
        subtotals = [a for a in annotations if a.annotation_type == "category_subtotal"]
        notes = [a for a in annotations if a.annotation_type == "note"]
        self.assertEqual([a.amount for a in subtotals], [211.91, 684.64])
        self.assertEqual(len(notes), 1)
        self.assertIn("Chloe owes", notes[0].text)

    def test_single_normal_subtotal_is_not_category_markup(self):
        text = """ITEM A 10.00
SUBTOTAL 10.00
GST 0.50
TOTAL 10.50
"""
        annotations = extract_receipt_annotations(text)
        self.assertFalse(any(a.annotation_type == "category_subtotal" for a in annotations))


if __name__ == "__main__":
    unittest.main()
