from pathlib import Path


SCRIPT = Path("scripts/deployment_status.sh")


def test_deployment_status_covers_ci_argocd_jobs_and_pods():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'gh run list --branch main --limit "$run_limit"' in text
    assert '"STATUS" "TITLE" "BRANCH" "COMMIT" "RUN ID" "AGE"' in text
    assert '(printf "%.7s" .headSha)' in text
    assert '{{if eq .conclusion ""}}{{tablerow .status' in text
    assert 'argocd app get "$argo_app" --grpc-web -o json' in text
    assert "Revision:      \\(.status.sync.revision)" in text
    assert "Sync Status:   \\(.status.sync.status)" in text
    assert "Health Status: \\(.status.health.status)" in text
    assert 'LEDGER_DEPLOYMENT:-ledger-web' in text
    assert 'docker buildx imagetools inspect "$configured_image"' in text
    assert "Pod running digest:" in text
    assert "Match:               %s" in text
    assert 'get jobs --sort-by=.metadata.creationTimestamp' in text
    assert 'get pods --sort-by=.metadata.name' in text


def test_deployment_status_has_safe_configurable_defaults():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'RUN_LIMIT:-3' in text
    assert 'ARGO_APP:-ledger' in text
    assert 'KUBE_NAMESPACE:-home-budget' in text
    assert "delete" not in text
    assert "apply" not in text
