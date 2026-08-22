import json
from pathlib import Path
from unittest.mock import patch

from home_budget_pipeline.receipts import ingest
from home_budget_pipeline.web import app as web_app


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sobeys_receipt_41_ocr.json"


def _fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_receipt_41_filename_populates_missing_receipt_info():
    fixture = _fixture()
    path = Path(fixture["filename"])
    expected = fixture["expected"]

    assert ingest.receipt_info_from_filename(path) == (
        expected["transaction_date"],
        expected["merchant"],
        expected["total"],
    )

    with patch.object(ingest, "extract_page_text", return_value=[fixture["ocr_text"]]), patch.object(
        ingest, "sha256_file", return_value="receipt-41"
    ):
        receipt = ingest.parse_scan(path)

    assert receipt.transaction_date == expected["transaction_date"]
    assert receipt.merchant == expected["merchant"]
    assert receipt.total == expected["total"]


def test_receipt_41_item_name_corrections():
    for correction in _fixture()["item_corrections"]:
        items = ingest.extract_items(correction["ocr_line"])
        assert [(item.item_name, item.line_total) for item in items] == [
            (correction["item_name"], correction["line_total"])
        ]


def test_receipt_41_post_ocr_display():
    fixture = _fixture()
    expected = fixture["expected"]
    correction = fixture["item_corrections"][0]
    text = web_app._post_ocr_text(
        {
            "source_reference": fixture["filename"],
            "merchant": "GROCERY",
            "total": str(expected["total"]),
            "payment_method": expected["payment_method"],
        },
        {
            "items": tuple(
                {
                    "item_name": correction["ocr_line"].split(" $")[0],
                    "line_total": str(correction["line_total"]),
                    "product_description": correction["product_description"],
                }
                for _ in range(2)
            )
        },
    )
    for expected_line in fixture["post_ocr_lines"]:
        assert expected_line in text


def test_receipt_41_tracks_unresolved_items_and_parser_noise():
    fixture = _fixture()
    assert fixture["items_to_find"]
    assert all(item["ocr_names"] and item["line_totals"] for item in fixture["items_to_find"])
    assert fixture["parser_noise"]
