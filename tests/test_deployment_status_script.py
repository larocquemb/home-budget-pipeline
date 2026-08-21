from pathlib import Path


SCRIPT = Path("scripts/deployment_status.sh")


def test_deployment_status_covers_ci_argocd_jobs_and_pods():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'gh run list --limit "$run_limit"' in text
    assert "'TITLE' 'BRANCH' 'PR' 'COMMIT' 'STATUS' 'COMPLETED' 'RUN ID'" in text
    assert ".headSha[0:7]" in text
    assert 'commits/$full_commit/pulls' in text
    assert "fromdateiso8601" in text
    assert '"\\($age / 60 | floor)m"' in text
    assert '"\\($age / 3600 | floor)h"' in text
    assert '"\\($age / 86400 | floor)d"' in text
    assert 'argocd app get "$argo_app" --grpc-web' in text
    assert "Target:|Sync Status:|Health Status:" in text
    assert 'get jobs --sort-by=.metadata.creationTimestamp' in text
    assert 'get pods --sort-by=.metadata.creationTimestamp' in text


def test_deployment_status_has_safe_configurable_defaults():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'RUN_LIMIT:-3' in text
    assert 'ARGO_APP:-ledger' in text
    assert 'KUBE_NAMESPACE:-home-budget' in text
    assert "delete" not in text
    assert "apply" not in text
