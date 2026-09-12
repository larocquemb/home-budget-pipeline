from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_receipt_processor_cronjob_contract():
    text = (ROOT / "k8s" / "receipt-processor-cronjob.yaml").read_text()
    assert "kind: CronJob" in text
    assert "concurrencyPolicy: Forbid" in text
    assert "- ledger" in text
    assert "- receipts" in text
    assert "- process" in text
    assert "--workers" in text and '"1"' in text
    assert "HOME_BUDGET_PADDLE_OCR" in text
    assert 'value: "true"' in text
    assert "claimName: home-budget-data" in text
    assert "name: postgres-secret" in text
    assert 'cpu: "2"' in text
    assert "memory: 4Gi" in text
    assert "memory: 8Gi" in text
    assert "fsGroup: 1000" in text
    assert "fsGroupChangePolicy: OnRootMismatch" in text


def test_kustomization_includes_receipt_processor():
    text = (ROOT / "k8s" / "kustomization.yaml").read_text()
    assert "receipt-processor-cronjob.yaml" in text
