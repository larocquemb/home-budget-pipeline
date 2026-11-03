from uuid import uuid4
from fastapi.testclient import TestClient
import pytest
from home_budget_pipeline.web import receipt_app
from home_budget_pipeline.web.app import app,query_service,authenticated_identity,BASE_PATH
from home_budget_pipeline.receipts import queue_ingest


def test_reprocess_requires_identity_action_and_receipt_and_queues_same_retry(monkeypatch):
    calls=[]
    class Service:
        records=[{'source_reference':'synthetic/receipt.pdf','source_archived':False,'discarded':False}]
        def _fetch(self,*args):return self.records
    service=Service()
    app.dependency_overrides[query_service]=lambda:service
    monkeypatch.setenv('RABBITMQ_URL','amqp://unused')
    monkeypatch.setattr(queue_ingest,'publish_reprocess_message',lambda message,*args:calls.append(message))
    body={'request_id':str(uuid4())};url=f'{BASE_PATH}/api/receipts/{"a"*64}/reprocess'
    try:
        with TestClient(app) as client:
            assert client.post(url,json=body).status_code==401
            app.dependency_overrides[authenticated_identity]=lambda:{'user':'test'}
            assert client.post(url,json=body).status_code==403
            headers={'X-Ledger-Action':'reprocess-receipt'}
            for _ in range(2):
                response=client.post(url,json=body,headers=headers)
                assert response.status_code==202
                assert response.json()['request_id']==body['request_id']
            assert calls[0].to_bytes()==calls[1].to_bytes()
            assert calls[0].version==2
            assert client.post(url.replace('a'*64,'invalid'),json=body,headers=headers).status_code==422
            service.records=[]
            assert client.post(url,json=body,headers=headers).status_code==404
            service.records=[{'source_reference':'synthetic/receipt.pdf','discarded':True}]
            assert client.post(url,json=body,headers=headers).status_code==409
    finally:app.dependency_overrides.clear()


@pytest.mark.parametrize('extraction,deleted,expected',[('review',False,True),('complete',False,False),('review',True,False)])
def test_review_status_has_reprocess_control(monkeypatch,extraction,deleted,expected):
    data={'source_reference':'synthetic/receipt.pdf','evidence_id':1,'expense_pk':None,
          'processing_status':'review_required','extraction_status':extraction,'deleted':deleted,'evidence':{},'expense':{}}
    monkeypatch.setattr(receipt_app,'_receipt_detail',lambda *a:data)
    html=receipt_app.receipt_page('a'*64,service=object(),identity={'user':'test'})
    assert ('id="reprocess-receipt"' in html)==expected


def test_publication_failure_reports_retry_without_exposing_connection(monkeypatch):
    class Service:
        def _fetch(self,*args):return [{'source_reference':'synthetic/receipt.pdf'}]
    app.dependency_overrides[query_service]=lambda:Service()
    app.dependency_overrides[authenticated_identity]=lambda:{'user':'test'}
    monkeypatch.setenv('RABBITMQ_URL','amqp://private-credentials')
    def fail(*args):raise RuntimeError('amqp://private-credentials')
    monkeypatch.setattr(queue_ingest,'publish_reprocess_message',fail)
    try:
        with TestClient(app) as client:
            url=f'{BASE_PATH}/api/receipts/{"a"*64}/reprocess'
            response=client.post(url,json={'request_id':str(uuid4())},headers={'X-Ledger-Action':'reprocess-receipt'})
            assert response.status_code==503
            assert 'retry' in response.json()['detail']
            assert 'private-credentials' not in response.text
    finally:app.dependency_overrides.clear()


@pytest.mark.parametrize('state,label',[('queued','Queued for reprocessing'),('heartbeat','Reprocessing'),('results_published','Saving results')])
def test_refreshed_detail_displays_current_request_above_old_review(monkeypatch,state,label):
    from datetime import datetime,timezone
    from home_budget_pipeline.web.receipt_fields import receipt_status
    now=datetime.now(timezone.utc)
    data={'source_reference':'synthetic/receipt.pdf','evidence_id':1,'expense_pk':None,
          'processing_status':'review_required','extraction_status':'review','evidence':{},'expense':{},
          'latest_request_id':str(uuid4()),'request_status':state,'request_observed_at':now,'request_submitted_at':now}
    monkeypatch.setattr(receipt_app,'_receipt_detail',lambda *a:data)
    html=receipt_app.receipt_page('a'*64,service=object(),identity={'user':'test'})
    assert label in html
    assert 'id="reprocess-receipt"' not in html
    data['request_completed_at']=now
    assert receipt_status(data)=='review_required'
    html=receipt_app.receipt_page('a'*64,service=object(),identity={'user':'test'})
    assert 'Latest request saved' in html and 'id="reprocess-receipt"' in html


def test_processing_status_expires_without_claiming_completion():
    from datetime import datetime,timezone,timedelta
    from home_budget_pipeline.web.receipt_fields import receipt_status
    now=datetime.now(timezone.utc)
    receipt={'latest_request_id':str(uuid4()),'request_status':'heartbeat',
             'request_observed_at':now-timedelta(minutes=5),'processing_status':'review_required'}
    assert receipt_status(receipt)=='progress_unknown'


def test_duplicate_reprocess_is_rejected_while_current_request_active(monkeypatch):
    from datetime import datetime,timezone
    class Service:
        def _fetch(self,*args):return [{'source_reference':'synthetic/receipt.pdf',
            'latest_request_id':str(uuid4()),'request_status':'active','request_observed_at':datetime.now(timezone.utc)}]
    app.dependency_overrides[query_service]=lambda:Service()
    app.dependency_overrides[authenticated_identity]=lambda:{'user':'test'}
    def unexpected(*args):raise AssertionError('Should not publish another request')
    monkeypatch.setattr(queue_ingest,'publish_reprocess_message',unexpected)
    try:
        with TestClient(app) as client:
            response=client.post(f'{BASE_PATH}/api/receipts/{"a"*64}/reprocess',json={'request_id':str(uuid4())},headers={'X-Ledger-Action':'reprocess-receipt'})
            assert response.status_code==409 and 'already reprocessing' in response.json()['detail']
    finally:app.dependency_overrides.clear()


def test_committed_latest_request_uses_new_extraction_instead_of_old_status():
    from home_budget_pipeline.web.receipt_fields import receipt_status
    receipt={'latest_request_id':str(uuid4()),'request_completed_at':'saved',
             'processing_status':'review_required','extraction_status':'complete'}
    assert receipt_status(receipt)=='succeeded'
