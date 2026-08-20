from pathlib import Path


def test_ledger_web_uses_database_url_and_read_only_receipt_mount():
    text = Path("k8s/ledger-web-service.yaml").read_text()
    assert "name: DATABASE_URL" in text
    assert "key: DATABASE_URL" in text
    assert "name: RECEIPT_SOURCE_ROOT" in text
    assert "value: /data/receipts/raw/scanned/inbox" in text
    assert "claimName: home-budget-data" in text
    assert "mountPath: /data" in text
    assert "readOnly: true" in text


def test_ledger_web_has_distinct_liveness_and_readiness_probes():
    text = Path("k8s/ledger-web-service.yaml").read_text()
    assert "path: /ledger/health" in text
    assert "path: /ledger/ready" in text


def test_web_run_documentation_covers_local_and_kubernetes_usage():
    text = Path("README.md").read_text()
    assert "## 11. Ledger Web Application" in text
    assert "export DATABASE_URL=" in text
    assert "RECEIPT_SOURCE_ROOT" in text
    assert "home-budget-ledger" in text
    assert "home-budget-data" in text
    assert "/ledger/ready" in text
