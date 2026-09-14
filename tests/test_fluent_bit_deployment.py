import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _render(path: str) -> dict[tuple[str, str], dict]:
    if not shutil.which("kubectl"):
        pytest.skip("kubectl is not installed")
    rendered = subprocess.check_output(
        ["kubectl", "kustomize", str(ROOT / path)], text=True
    )
    return {
        (obj["kind"], obj["metadata"]["name"]): obj
        for obj in yaml.safe_load_all(rendered)
    }


def test_fluent_bit_is_namespace_scoped_and_collects_only_home_budget_logs():
    resources = _render("deploy/fluent-bit")
    account = resources[("ServiceAccount", "fluent-bit")]
    role = resources[("Role", "fluent-bit")]
    daemonset = resources[("DaemonSet", "fluent-bit")]
    pod = daemonset["spec"]["template"]["spec"]
    container = pod["containers"][0]
    config = resources[("ConfigMap", "fluent-bit-config")]["data"]

    assert account["metadata"]["namespace"] == "home-budget"
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["pods"],
            "verbs": ["get", "list", "watch"],
        }
    ]
    assert ("ClusterRole", "fluent-bit") not in resources
    assert ("ClusterRoleBinding", "fluent-bit") not in resources
    assert "*_home-budget_*.log" in config["fluent-bit.conf"]
    assert "*_home-budget_fluent-bit-*.log" in config["fluent-bit.conf"]
    assert "multiline.parser           cri" in config["fluent-bit.conf"]
    assert "multiline.parser           python" in config["fluent-bit.conf"]
    assert next(
        volume for volume in pod["volumes"] if volume["name"] == "varlog"
    )["hostPath"] == {"path": "/var/log", "type": "Directory"}
    assert container["image"] == "cr.fluentbit.io/fluent/fluent-bit:4.2.0"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_fluent_bit_uses_native_loki_mtls_with_bounded_labels_and_buffering():
    resources = _render("deploy/fluent-bit")
    config = resources[("ConfigMap", "fluent-bit-config")]["data"]
    fluent = config["fluent-bit.conf"]
    lua = config["filters.lua"]
    labels_line = next(
        line for line in fluent.splitlines() if "collection=fluent-bit" in line
    )
    daemonset = resources[("DaemonSet", "fluent-bit")]
    container = daemonset["spec"]["template"]["spec"]["containers"][0]

    assert "Name                       loki" in fluent
    assert "URI                        /loki/api/v1/push" in fluent
    assert "collection=fluent-bit" in fluent
    assert "Auto_Kubernetes_Labels     Off" in fluent
    assert "TLS.Verify                 On" in fluent
    assert "TLS.Verify_Hostname        On" in fluent
    assert "TLS.Crt_File               /fluent-bit/tls/tls.crt" in fluent
    assert "TLS.Key_File               /fluent-bit/tls/tls.key" in fluent
    assert "Retry_Limit                False" in fluent
    assert "storage.total_limit_size   256M" in fluent
    assert "storage.metrics            on" in fluent
    assert "receipt=" not in labels_line
    assert "request_id" not in labels_line
    assert "run_uuid" not in labels_line
    assert "pass_id" not in labels_line
    assert "postgresql://[REDACTED]" in lua
    assert "labels[\"app\"]" in lua
    assert "labels[\"app.kubernetes.io/name\"]" in lua
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/v2/health"
    assert container["livenessProbe"]["httpGet"]["path"] == "/"
    assert container["resources"]["limits"] == {
        "cpu": "300m",
        "memory": "256Mi",
    }
    assert resources[("Service", "fluent-bit")]["spec"]["ports"] == [
        {"name": "metrics", "port": 2020, "targetPort": "metrics"}
    ]


def test_fluent_bit_tls_template_is_data_free_and_not_deployed():
    template_path = ROOT / "deploy/fluent-bit/fluent-bit-loki-tls.example.yaml"
    template = yaml.safe_load(template_path.read_text())
    kustomization = (ROOT / "deploy/fluent-bit/kustomization.yaml").read_text()

    assert template["metadata"]["name"] == "fluent-bit-loki-tls"
    assert set(template["stringData"]) == {"ca.crt", "tls.crt", "tls.key"}
    assert "fluent-bit-loki-tls.example.yaml" not in kustomization


def test_rabbitmq_overlays_add_fluent_bit_without_removing_private_access():
    standard = _render("deploy/rabbitmq-logging")
    private = _render("deploy/rabbitmq-private-logging")

    for resources in (standard, private):
        assert ("StatefulSet", "rabbitmq") in resources
        assert ("Deployment", "receipt-worker") in resources
        assert ("DaemonSet", "fluent-bit") in resources
    assert ("Ingress", "ledger-private") in private


def test_local_overlay_routes_fluent_bit_to_disposable_loki():
    resources = _render("deploy/local-logging/fluent-bit")
    config = resources[("ConfigMap", "fluent-bit-config")]["data"]

    assert config["LOKI_HOST"] == "loki-local.home-budget.svc.cluster.local"
    assert config["LOKI_TLS_VHOST"] == "loki"
    assert config["LOKI_CLUSTER"] == "kind-home-budget-logging"
    assert config["LOKI_ENVIRONMENT"] == "local"
    endpoint_slice = resources[("EndpointSlice", "loki-local")]
    assert endpoint_slice["metadata"]["labels"]["kubernetes.io/service-name"] == (
        "loki-local"
    )
    assert endpoint_slice["addressType"] == "IPv4"
    assert endpoint_slice["endpoints"][0]["addresses"] == ["192.0.2.1"]
