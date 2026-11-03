import json
from urllib.parse import urlparse,parse_qs

from home_budget_pipeline.web.receipt_graph import latest_receipt_trace_url
from home_budget_pipeline.web import receipt_app


def test_latest_receipt_trace_search_needs_no_run_or_trace_id_and_escapes_values(monkeypatch):
    monkeypatch.setenv('OTEL_DEPLOYMENT_ENVIRONMENT','development')
    reference='synthetic/a"b.pdf'
    url=latest_receipt_trace_url(reference)
    panes=json.loads(parse_qs(urlparse(url).query)['panes'][0])
    query=panes['A']['queries'][0]
    assert query['queryType']=='traceql'
    assert 'span.receipt.source_reference = '+json.dumps(reference) in query['query']
    assert 'resource.deployment.environment.name = "development"' in query['query']
    assert 'with (most_recent=true)' in query['query']
    assert panes['A']['range']=={'from':'now-1h','to':'now'}
    monkeypatch.setenv('GRAPH_GRAFANA_URL','javascript:invalid')
    assert latest_receipt_trace_url(reference) is None


def test_receipt_detail_offers_latest_traces_without_stored_run_identity(monkeypatch):
    data={'source_reference':'synthetic/example.pdf','evidence_id':1,'expense_pk':None,
          'processing_status':'queued','extraction_status':None,'evidence':{},'expense':{}}
    monkeypatch.setattr(receipt_app,'_receipt_detail',lambda *a:data)
    html=receipt_app.receipt_page('a'*64,service=object(),identity={'user':'test'})
    assert '>Latest traces</a>' in html
    assert 'source_reference' in html and 'most_recent' in html
