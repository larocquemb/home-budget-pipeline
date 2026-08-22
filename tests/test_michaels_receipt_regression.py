import json
import os
import shutil
from pathlib import Path

import pytest

from home_budget_pipeline.receipts import ingest
from home_budget_pipeline.receipts.datetime import extract_transaction_datetime
from home_budget_pipeline.receipts.parallel_ingest import parse_scans_parallel
from home_budget_pipeline.receipts.payment import extract_payment_provenance


def test_michaels_return_receipt_multi_pass_regression():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "michaels_return_ocr.json").read_text()
    )
    text = fixture["primary"]
    for alternative in fixture["alternatives"]:
        text = ingest._supplement_missing_receipt_summary(text, alternative)

    subtotal, tax, total = ingest.extract_totals(text)
    payment = extract_payment_provenance(text)
    items = ingest.normalize_item_signs(ingest.extract_items(text), total)
    expected = fixture["expected"]

    assert ingest.extract_merchant(text) == expected["merchant"]
    assert extract_transaction_datetime(text) == expected["transaction_datetime"]
    assert (subtotal, tax, total) == (
        expected["subtotal"], expected["tax"], expected["total"]
    )
    assert (payment.payment_method, payment.card_last4) == (
        expected["payment_method"], expected["card_last4"]
    )
    assert [item.item_name for item in items] == expected["item_names"]
    assert [item.line_total for item in items] == expected["item_totals"]

    receipt = ingest.ScannedReceipt(
        path="fixture.pdf",
        source_reference="fixture.pdf",
        source_sha256="fixture",
        merchant=expected["merchant"],
        transaction_date="2026-05-31",
        receipt_id=None,
        subtotal=subtotal,
        tax=tax,
        total=total,
        payment_method=payment.payment_method,
        card_last4=payment.card_last4,
        items=items,
        text=text,
    )
    receipt.extraction_confidence = ingest.confidence_for(receipt)
    assert ingest.status_for(receipt) == "complete"


@pytest.mark.integration
def test_actual_michaels_receipt_ocr_finds_visible_values(tmp_path, record_property):
    receipt_path = Path(
        os.getenv(
            "MICHAELS_RECEIPT_TEST_PATH",
            "/Volumes/files/brownrook/home-budget/receipts/raw/scanned/inbox/2026-08-11/receipt_0009.pdf",
        )
    )
    if not receipt_path.is_file():
        pytest.skip(
            "INFO: private Michaels OCR fixture is unavailable; set "
            "MICHAELS_RECEIPT_TEST_PATH to run the golden OCR test"
        )
    if not shutil.which("tesseract"):
        pytest.skip("INFO: Tesseract is unavailable; golden OCR test was not run")

    record_property("ocr_fixture", str(receipt_path))
    receipt = parse_scans_parallel(
        [receipt_path],
        receipt_path.parent,
        workers=1,
        cache_dir=tmp_path / "ocr-cache",
        refresh=True,
    )[0]

    assert receipt.merchant.upper() == "MICHAELS"
    assert receipt.transaction_datetime == "2026-05-31T10:07:00"
    assert (receipt.subtotal, receipt.tax, receipt.total) == (-37.98, -4.56, -42.54)
    assert (receipt.payment_method, receipt.card_last4) == ("Visa", "9809")
    assert len(receipt.items) == 3
    assert [item.line_total for item in receipt.items] == [-18.99, -18.98, -0.01]
    assert all("PUFF" in item.item_name.upper() for item in receipt.items)
    assert receipt.extraction_status == "complete"
    assert receipt.review_reasons == []
