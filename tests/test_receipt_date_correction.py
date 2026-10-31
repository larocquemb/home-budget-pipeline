import pytest

from home_budget_pipeline.receipts.date_correction import normalize_datetime


@pytest.mark.parametrize('value', ['not-a-date', '2026-02-30', '2026-07-09T11:25Z',
                                 '2026-07-09T11:25+01:00', None])
def test_date_correction_requires_valid_receipt_local_date(value):
    with pytest.raises(ValueError):
        normalize_datetime(value)


def test_date_correction_keeps_supplied_precision_without_guessing_timezone():
    assert normalize_datetime('2026-07-09').isoformat() == '2026-07-09T00:00:00'
    assert normalize_datetime('2026-07-09T11:25').isoformat() == '2026-07-09T11:25:00'
