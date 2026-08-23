import pytest

from home_budget_pipeline.receipt_enrichment import receipt_patterns


def test_numeric_receipt_selector_uses_zero_padded_scanned_filename():
    assert receipt_patterns("1") == ("%receipt_0001.pdf%", "%receipt_1.pdf%")


def test_receipt_selector_accepts_filename_or_path():
    assert receipt_patterns("2026-08-11/receipt_0023.pdf") == ("%receipt_0023.pdf%",)


def test_receipt_selector_rejects_blank_value():
    with pytest.raises(ValueError, match="receipt selector is required"):
        receipt_patterns("   ")
