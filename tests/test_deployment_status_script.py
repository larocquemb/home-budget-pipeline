from pathlib import Path


SCRIPT = Path("scripts/deployment_status.sh")


def test_deployment_status_covers_ci_argocd_jobs_and_pods():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'gh run list --repo "$github_repo" --branch main --limit "$run_limit"' in text
    assert '["STATUS", "TITLE", "BRANCH", "COMMIT", "RUN ID", "AGE"]' in text
    assert '.headSha[0:7]' in text
    assert '"\\($age / 60 | floor)m\\($age % 60)s"' in text
    assert '"\\($age / 3600 | floor)h' in text
    assert '"\\($age / 86400 | floor)d' in text
    assert 'argocd app get "$argo_app" --core -o json' in text
    assert ".status.sync.revision[0:7]" in text
    assert "Revision:      %s -> Application: %.7s" in text
    assert "Sync Status:   %s" in text
    assert "Health Status: %s" in text
    assert 'LEDGER_DEPLOYMENT:-ledger-web' in text
    assert 'docker buildx imagetools inspect "$configured_image"' in text
    assert 'application_commit="${application_commit:0:7}"' in text
    assert "GHCR digest:        %.7s" in text
    assert "Pod digest:         %.7s" in text
    assert "Match:              %s" in text
    assert 'get jobs --sort-by=.metadata.creationTimestamp' in text
    assert 'get pods -o json' in text
    assert '["NAME", "READY", "STATUS", "RESTARTS", "AGE", "COMMIT"]' in text
    assert 'home-budget-pipeline:' in text


def test_deployment_status_has_safe_configurable_defaults():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'RUN_LIMIT:-3' in text
    assert 'GITHUB_REPO:-larocquemb/home-budget-pipeline' in text
    assert 'ARGO_APP:-ledger' in text
    assert 'KUBE_NAMESPACE:-home-budget' in text
    assert 'KUBECONFIG:-$HOME/.kube/config-brownrook' in text
    assert "delete" not in text
    assert "apply" not in text
