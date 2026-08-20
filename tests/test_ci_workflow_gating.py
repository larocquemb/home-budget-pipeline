from pathlib import Path


WORKFLOW = Path(".github/workflows/ci.yml")


def workflow_text() -> str:
    return WORKFLOW.read_text()


def test_feature_branch_pushes_do_not_trigger_ci_workflow():
    text = workflow_text()
    assert "- 'KAN-*'" not in text
    assert "push:\n    branches:\n      - main" in text


def test_pull_requests_still_trigger_validation():
    text = workflow_text()
    assert "pull_request:" in text
    assert "run: python -m pytest -v" in text


def test_build_and_push_requires_push_to_main():
    text = workflow_text()
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in text


def test_deployment_pointer_is_committed_only_to_main():
    text = workflow_text()
    assert "git push origin HEAD:main" in text
    assert "git push origin HEAD:${GITHUB_REF_NAME}" not in text
