from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_receipt_processor_cronjob_contract():
    text = (ROOT / "k8s" / "receipt-processor-cronjob.yaml").read_text()
    assert "kind: CronJob" in text
    assert "concurrencyPolicy: Forbid" in text
    assert "home-budget-process-receipts" in text
    assert "--workers" in text and '"2"' in text
    assert "claimName: home-budget-data" in text
    assert "name: postgres-secret" in text
    assert 'cpu: "2"' in text
    assert "memory: 2Gi" in text


def test_kustomization_includes_receipt_processor():
    text = (ROOT / "k8s" / "kustomization.yaml").read_text()
    assert "receipt-processor-cronjob.yaml" in text
