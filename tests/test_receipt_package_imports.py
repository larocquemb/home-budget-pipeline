from home_budget_pipeline.receipts import backlog_ingest, ingest, parallel_ingest


def test_receipt_processors_use_canonical_ingest_module():
    assert backlog_ingest.scan is ingest
    assert parallel_ingest.scan is ingest
    assert hasattr(backlog_ingest.scan, "_db_connect")
    assert hasattr(parallel_ingest.scan, "_db_connect")
