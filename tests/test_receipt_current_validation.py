"""Current allocations override stale complete labels, preserving saved history."""
import pytest
from home_budget_pipeline.receipts.allocation import assess
from home_budget_pipeline.web.receipt_app import _receipt_detail


@pytest.mark.parametrize('stored,amount,expected', [
    ('complete', '10.00', 'complete'),
    ('complete', '12.00', 'review'),
    ('review', '10.00', 'review'),
    ('unreadable', '10.00', 'unreadable'),
])
def test_current_allocation_check_controls_effective_status(stored, amount, expected):
    class Service:
        calls = 0

        def _fetch(self, sql, params=()):
            if 'WHERE r.source_sha256' in sql:
                return [{'source_sha256':'a'*64, 'evidence_id':1, 'expense_pk':2}]
            return []

        def receipt_evidence(self, identity):
            return {'extraction_status':stored, 'merchant':'Synthetic Shop'}

        def expense_detail(self, identity):
            return {'extraction_status':stored, 'items':[]}

        def receipt_allocation(self, sha):
            self.calls += 1
            return {'assessment':assess([{'line_total':amount}], '10.00', '0.00', '10.00')}

    service = Service()
    receipt = _receipt_detail(service, 'a'*64)
    assert receipt['extraction_status'] == expected
    assert receipt['stored_extraction_status'] == stored
    assert receipt['evidence']['extraction_status'] == stored
    assert service.calls == 1
    assert receipt['allocation']['assessment']['reconciled'] == (amount == '10.00')
