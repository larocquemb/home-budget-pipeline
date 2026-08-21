from pathlib import Path


def test_argocd_presync_hook_resets_receipt_processing_state():
    manifest = Path("k8s/receipt-processing-reset-job.yaml").read_text(encoding="utf-8")

    assert "argocd.argoproj.io/hook: PreSync" in manifest
    assert "BeforeHookCreation,HookSucceeded" in manifest
    assert "receipt_processing.sql" in manifest
    assert "DATABASE_URL" in manifest
    assert "postgres-secret" in manifest


def test_kustomization_includes_receipt_processing_reset_hook():
    kustomization = Path("k8s/kustomization.yaml").read_text(encoding="utf-8")

    assert "receipt-processing-reset-job.yaml" in kustomization
