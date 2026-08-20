from home_budget_pipeline.web import app as web_app
from home_budget_pipeline.web.queries import LedgerQueryService, Page


IDENTITY = {"user": "Paul", "email": "paul@example.com"}


class FakeProcessingService:
    def receipt_processing_status(self, **kwargs):
        return Page(({
            "source_reference": "receipt_0003.pdf",
            "status": "failed",
            "attempts": 2,
            "last_error": "OCR failed",
            "first_attempted_at": "2026-08-20 18:00",
            "last_attempted_at": "2026-08-20 18:15",
            "completed_at": None,
        },), kwargs.get("limit", 100), kwargs.get("offset", 0))


class CapturingQueryService(LedgerQueryService):
    def __init__(self):
        self.sql = ""
        self.params = ()

    def _fetch(self, sql, params=()):
        self.sql = sql
        self.params = params
        return ()


def test_dashboard_links_receipt_processing_status():
    text = web_app.ledger_home(identity=IDENTITY)
    assert f'{web_app.BASE_PATH}/receipt-processing' in text


def test_receipt_processing_page_shows_failure_and_attempts():
    text = web_app.receipt_processing_page(
        service=FakeProcessingService(), identity=IDENTITY, limit=100, offset=0
    )
    assert "receipt_0003.pdf" in text
    assert "failed" in text
    assert "OCR failed" in text
    assert ">2<" in text


def test_receipt_processing_query_uses_status_table_and_pagination():
    service = CapturingQueryService()
    page = service.receipt_processing_status(limit=25, offset=50)
    assert "budget.receipt_processing_status" in service.sql
    assert service.params == (25, 50)
    assert page.limit == 25
    assert page.offset == 50
