import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _render(path: str) -> dict[tuple[str, str], dict]:
    if not shutil.which("kubectl"):
        pytest.skip("kubectl is not installed")
    rendered = subprocess.check_output(["kubectl", "kustomize", str(ROOT / path)], text=True)
    return {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(rendered)}


def test_external_reader_is_namespace_scoped_and_has_no_secret_access():
    resources = _render("k8s")
    account = resources[("ServiceAccount", "external-log-reader")]
    role = resources[("Role", "external-log-reader")]
    binding = resources[("RoleBinding", "external-log-reader")]

    assert account["metadata"]["namespace"] == "home-budget"
    assert account["automountServiceAccountToken"] is False
    assert role["metadata"]["namespace"] == "home-budget"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    ]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "external-log-reader",
    }


def test_external_token_template_is_not_deployed_or_populated():
    template = yaml.safe_load((ROOT / "k8s/external-log-reader-token.example.yaml").read_text())
    kustomization = (ROOT / "k8s/kustomization.yaml").read_text()

    assert template["type"] == "kubernetes.io/service-account-token"
    assert template["metadata"]["annotations"]["kubernetes.io/service-account.name"] == "external-log-reader"
    assert "data" not in template
    assert "stringData" not in template
    assert "external-log-reader-token.example.yaml" not in kustomization


def test_external_alloy_uses_kubernetes_api_and_verified_loki_tls():
    config = (ROOT / "deploy/external-logging/config.alloy").read_text()

    assert config.count('kubeconfig_file = "/etc/alloy/home-budget.kubeconfig"') == 2
    assert 'names = ["home-budget"]' in config
    assert 'cluster     = "brownrook-k3s1"' in config
    assert 'url = "https://127.0.0.1:3100/loki/api/v1/push"' in config
    assert 'ca_file     = "/etc/alloy/brown-rook-root-ca.crt"' in config
    assert 'server_name = "monitoring.idc.brownrook.net"' in config
    assert 'min_version = "TLS12"' in config
    assert "insecure_skip_verify" not in config
    assert "wal {\n    enabled = true" in config


def test_exported_kubeconfigs_are_ignored():
    assert "*.kubeconfig" in (ROOT / ".gitignore").read_text().splitlines()
