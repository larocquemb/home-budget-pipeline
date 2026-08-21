from home_budget_pipeline.web import app as web_app
from home_budget_pipeline.web.queries import Page


IDENTITY = {"user": "Paul", "email": "paul@example.com"}


class FakeService:
    def analytics_expenses(self, **kwargs):
        return Page(({
            "expense_pk": 42,
            "source": "costco",
            "order_date": "2026-08-20",
            "transaction_datetime": None,
            "store_name": "Costco",
            "account_name": "Aventura",
            "expense_total": "295.47",
            "extraction_status": "complete",
            "requires_review": False,
        },), kwargs.get("limit", 50), kwargs.get("offset", 0), 101)

    def category_spend(self, **kwargs):
        return Page(({
            "expense_date": "2026-08-20",
            "budget_category": "Groceries",
            "category_group_name": "Household",
            "category_amount": "250.00",
            "item_count": 10,
            "expense_count": 1,
        },), kwargs.get("limit", 100), kwargs.get("offset", 0))

    def review_queue(self, **kwargs):
        return Page((), kwargs.get("limit", 50), kwargs.get("offset", 0))

    def extraction_audit(self, **kwargs):
        return Page(({
            "expense_pk": 42,
            "evidence_id": 7,
            "source_reference": "receipt.pdf",
            "order_date": "2026-08-20",
            "store_name": "Costco",
            "expense_total": "295.47",
            "extraction_status": "complete",
            "item_count": 3,
        },), kwargs.get("limit", 100), kwargs.get("offset", 0))

    def pending_duplicates(self, **kwargs):
        return Page(({
            "id": 1,
            "left_evidence_id": 7,
            "right_evidence_id": 8,
            "score": 80,
            "disposition": "probable_duplicate",
            "item_similarity": 0.75,
            "reasons": ["merchant_match"],
        },), kwargs.get("limit", 50), kwargs.get("offset", 0))

    def transaction_reconciliation(self, **kwargs):
        return Page(({
            "transaction_id": 9,
            "transaction_date": "2026-08-20",
            "description": "COSTCO",
            "amount": "295.47",
            "payer": "Paul",
            "outcome": "matched",
            "score": 95,
            "expense_pk": 42,
        },), kwargs.get("limit", 50), kwargs.get("offset", 0))

    def expense_detail(self, expense_pk):
        if expense_pk != 42:
            return None
        return {
            "expense_pk": 42,
            "source": "costco",
            "order_date": "2026-08-20",
            "transaction_datetime": None,
            "store_name": "Costco",
            "account_name": "Aventura",
            "expense_total": "295.47",
            "requires_review": False,
            "items": ({"item_name": "Milk", "budget_category": "Groceries", "category_group_name": "Household", "line_total": "5.94", "category_source": "rule", "category_confidence": 1},),
            "evidence": ({"id": 7, "evidence_type": "scanned", "source_reference": "receipt.pdf", "transaction_datetime": "2026-08-20 10:00", "total": "295.47", "extraction_status": "complete"},),
        }

    def receipt_evidence(self, evidence_id):
        if evidence_id != 7:
            return None
        return {
            "id": 7,
            "expense_pk": 42,
            "evidence_type": "scanned",
            "source_reference": "receipt.pdf",
            "merchant": "Costco",
            "transaction_datetime": "2026-08-20 10:00",
            "total": "295.47",
            "extraction_status": "complete",
            "extraction_confidence": 0.99,
            "raw_text": "COSTCO RECEIPT",
        }


def test_dashboard_links_to_human_friendly_pages():
    text = web_app.ledger_home(identity=IDENTITY)
    assert f'{web_app.BASE_PATH}/expenses' in text
    assert f'{web_app.BASE_PATH}/duplicates' in text
    assert "/api/analytics/expenses" not in text


def test_expenses_page_has_filter_and_drilldown():
    text = web_app.expenses_page(service=FakeService(), identity=IDENTITY, limit=50, offset=0)
    assert "Merchant" in text
    assert "Costco" in text
    assert f'{web_app.BASE_PATH}/expenses/42' in text
    assert "Filter" in text
    assert "<strong>101</strong> expenses" in text
    assert text.index("Filter</button>") < text.index("<strong>101</strong> expenses") < text.index("</form>")
    assert "Page 1 of 3" in text
    assert "Next →" in text


def test_expense_detail_shows_items_and_evidence():
    text = web_app.expense_page(42, service=FakeService(), identity=IDENTITY)
    assert "Line items" in text
    assert "Milk" in text
    assert "Receipt evidence" in text
    assert f'{web_app.BASE_PATH}/evidence/7' in text


def test_extraction_audit_links_expense_and_evidence():
    text = web_app.extraction_audit_page(
        service=FakeService(), identity=IDENTITY, limit=100, offset=0, max_items=4
    )
    assert "Extraction audit" in text
    assert "receipt.pdf" in text
    assert f'{web_app.BASE_PATH}/expenses/42' in text
    assert f'{web_app.BASE_PATH}/evidence/7' in text


def test_evidence_page_embeds_cropped_pdf_preview_and_keeps_original_link():
    text = web_app.evidence_page(7, service=FakeService(), identity=IDENTITY)
    document_url = f'{web_app.BASE_PATH}/evidence/7/document'
    preview_url = f'{web_app.BASE_PATH}/evidence/7/preview'
    assert f'<img class="receipt-preview receipt-preview-image" src="{preview_url}"' in text
    assert "data-hover-zoom" in text
    assert "--zoom-x" in text
    assert "requestAnimationFrame" in text
    assert "image.offsetLeft" in text
    assert 'class="receipt-review-grid"' in text
    assert 'class="receipt-text"' in text
    assert "Open original receipt" in text
    assert f'href="{document_url}"' in text
    assert "COSTCO RECEIPT" in text


def test_duplicate_page_links_both_evidence_records():
    text = web_app.duplicates_page(service=FakeService(), identity=IDENTITY, limit=50, offset=0)
    assert f'{web_app.BASE_PATH}/evidence/7' in text
    assert f'{web_app.BASE_PATH}/evidence/8' in text


def test_transaction_page_links_matched_expense():
    text = web_app.transactions_page(service=FakeService(), identity=IDENTITY, limit=50, offset=0)
    assert "matched" in text
    assert f'{web_app.BASE_PATH}/expenses/42' in text
