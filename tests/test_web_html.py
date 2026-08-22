from home_budget_pipeline.web import app as web_app
from home_budget_pipeline.web.queries import Page


IDENTITY = {"user": "Paul", "email": "paul@example.com"}


class FakeService:
    def active_categories(self):
        return ("Groceries", "Indoor Supplies", "Outdoor Supplies")

    def category_rules(self, **kwargs):
        return ({"id": 8, "source": "costco", "merchant": "Costco", "match_type": "exact", "match_text": "towel", "category_name": "Indoor Supplies", "priority": 10, "is_active": True},)

    def category_rule_audit(self, **kwargs):
        return ({"created_at": "2026-08-21", "actor_email": "paul@example.com", "action": "created", "match_text": "towel", "old_category": "Groceries", "new_category": "Indoor Supplies", "affected_item_count": 1},)

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
            "items": ({"expense_item_id": 12, "item_name": "Milk", "budget_category": "Groceries", "category_group_name": "Household", "line_total": "5.94", "category_source": "rule", "category_confidence": 1},),
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
    assert 'href="/ledger/expenses?limit=50&amp;sort=expense_total&amp;direction=asc"' in text
    assert "Date ▼" in text


def test_expenses_page_preserves_filters_when_sorting_and_toggles_direction():
    text = web_app.expenses_page(
        service=FakeService(), identity=IDENTITY, limit=25, offset=0,
        source="costco", merchant="Regent", requires_review="true",
        sort="store_name", direction="asc",
    )

    assert "Merchant ▲" in text
    assert "source=costco" in text
    assert "merchant=Regent" in text
    assert "requires_review=true" in text
    assert "sort=store_name" in text
    assert "direction=desc" in text


def test_expense_detail_shows_items_and_evidence():
    text = web_app.expense_page(42, service=FakeService(), identity=IDENTITY)
    assert "Line items" in text
    assert "Milk" in text
    assert "Receipt evidence" in text
    assert f'{web_app.BASE_PATH}/evidence/7' in text


def test_expense_detail_category_dropdown_creates_override_action():
    text = web_app.expense_page(42, service=FakeService(), identity=IDENTITY)

    assert 'class="category-override-form" data-item-id="12"' in text
    assert '<option value="Groceries" selected>Groceries</option>' in text
    assert '<option value="Indoor Supplies">Indoor Supplies</option>' in text
    assert "Save override" in text
    assert f"{web_app.BASE_PATH}/api/category-overrides" in text
    assert "X-Ledger-Action" in text


def test_category_override_endpoint_saves_approved_rule():
    class OverrideService:
        def save_category_override(self, expense_item_id, category, *, actor_user, actor_email):
            assert expense_item_id == 12
            assert category == "Indoor Supplies"
            assert actor_user == "Paul"
            assert actor_email == "paul@example.com"
            return {"mapping_id": 8, "affected_items": 2}

    result = web_app.api_category_override(
        web_app.CategoryOverrideRequest(expense_item_id=12, category="Indoor Supplies"),
        x_ledger_action="category-override",
        service=OverrideService(),
        identity=IDENTITY,
    )

    assert result == {"mapping_id": 8, "affected_items": 2}


def test_category_rules_page_manages_rules_and_shows_history():
    text = web_app.category_rules_page(service=FakeService(), identity=IDENTITY, q=None)

    assert "Create rule" in text
    assert 'data-rule-id="8"' in text
    assert '<option value="Indoor Supplies" selected>Indoor Supplies</option>' in text
    assert "Recent changes" in text
    assert "paul@example.com" in text
    assert f"{web_app.BASE_PATH}/api/category-rules" in text


def test_category_rule_endpoint_records_signed_in_actor():
    class RuleService:
        def save_category_rule(self, **kwargs):
            assert kwargs["match_text"] == "TOWEL"
            assert kwargs["actor_user"] == "Paul"
            assert kwargs["actor_email"] == "paul@example.com"
            return {"mapping_id": 9, "audit_id": 10, "affected_items": 1}

    result = web_app.api_category_rule(
        web_app.CategoryRuleRequest(
            match_type="exact", match_text="TOWEL", category="Indoor Supplies"
        ),
        x_ledger_action="category-rule",
        service=RuleService(),
        identity=IDENTITY,
    )
    assert result["audit_id"] == 10


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
    assert "Parsed extraction after OCR" in text
    assert "Merchant: Costco" in text
    assert "Line items:" in text
    assert "Milk" in text
    assert "<summary>OCR text</summary>" in text


def test_duplicate_page_links_both_evidence_records():
    text = web_app.duplicates_page(service=FakeService(), identity=IDENTITY, limit=50, offset=0)
    assert f'{web_app.BASE_PATH}/evidence/7' in text
    assert f'{web_app.BASE_PATH}/evidence/8' in text


def test_transaction_page_links_matched_expense():
    text = web_app.transactions_page(service=FakeService(), identity=IDENTITY, limit=50, offset=0)
    assert "matched" in text
    assert f'{web_app.BASE_PATH}/expenses/42' in text
