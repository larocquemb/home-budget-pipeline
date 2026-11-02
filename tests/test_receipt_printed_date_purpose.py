"""Explicit dated payment blocks outrank promotion metadata and OCR fragments."""
from home_budget_pipeline.receipts.datetime import extract_transaction_datetime
from home_budget_pipeline.receipts.summary_evidence import resolve


def observation(text,number):
    return {'run_uuid':'synthetic','pass_id':number,'page_number':2,'status':'success','text':text}


def test_payment_date_and_time_are_not_replaced_by_contest_deadline():
    text = 'CARD MC\nDATE 07/12/2031\nTIME 09:42:13\nAUTH 123\nContest ends Jul 31 2031'
    assert extract_transaction_datetime(text) == '2031-07-12T09:42:13'


def test_separate_printed_date_corroborates_timed_day_glyph_without_voting():
    good = 'DATE 07/12/2031\nTIME 09:42:13\nAUTH 123\nTerm Tran Store\n07/12/31\n09:42:17\nContest ends Jul 31 2031'
    bad = 'DATE 07/17/2031\nAUTH 123\nTerm Tran Store\n07/17/31'
    result = resolve('',[observation(good,1),observation(bad,2)])['transaction_datetime']
    assert result['value'] == '2031-07-12T09:42:13'
    assert result['reason'] == 'timed_date_supported_by_separately_printed_short_date'
    assert any(c['value'] == '2031-07-17' for c in result['candidates'])


def test_conflicting_timed_readings_or_multiple_corroborated_dates_stay_in_review():
    a = 'DATE 07/12/2031\nTIME 09:42:13\nAUTH 123\n07/12/31'
    b = 'DATE 07/17/2031\nTIME 09:42:18\nAUTH 123\n07/17/31'
    for text in (b,b.replace('09:42:18','09:42:13')):
        result = resolve('',[observation(a,1),observation(text,2)])['transaction_datetime']
        assert result['value'] is None and result['status'] == 'conflict'
