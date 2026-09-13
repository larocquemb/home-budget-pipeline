from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "deploy" / "local-logging"


def test_local_stack_keeps_alloy_outside_kind_and_uses_verified_tls():
    compose = yaml.safe_load((LOCAL / "compose.yaml").read_text())
    services = compose["services"]
    alloy = services["alloy"]

    assert set(services) == {"alloy", "grafana", "loki"}
    assert alloy["networks"] == ["default", "kind"]
    assert "alloy" not in (LOCAL / "kustomization.yaml").read_text()
    assert services["loki"]["ports"] == ["127.0.0.1:13100:3100"]
    assert services["grafana"]["ports"] == ["127.0.0.1:13000:3000"]

    config = (LOCAL / "config.local.alloy").read_text()
    assert config.count('kubeconfig_file = "/etc/alloy/home-budget.kubeconfig"') == 2
    assert 'url = "https://loki:3100/loki/api/v1/push"' in config
    assert 'ca_file     = "/etc/alloy/tls/ca.crt"' in config
    assert 'server_name = "loki"' in config
    assert 'min_version = "TLS12"' in config
    assert 'environment = "local"' in config
    assert 'cluster     = "kind-home-budget-logging"' in config
    assert "insecure_skip_verify" not in config


def test_local_kind_runs_the_real_home_budget_web_application():
    deployment_documents = list(
        yaml.safe_load_all((LOCAL / "home-budget-local.yaml").read_text())
    )
    deployment = next(item for item in deployment_documents if item["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert container["image"] == "home-budget-pipeline:local"
    assert container["imagePullPolicy"] == "Never"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ledger/health"

    dockerfile = (LOCAL / "Dockerfile").read_text()
    assert "home_budget_pipeline.web.receipt_app:app" in dockerfile
    assert '"--access-log"' in dockerfile


def test_local_access_policy_matches_the_production_reader_boundary():
    resources = list(yaml.safe_load_all((LOCAL / "local-access.yaml").read_text()))
    by_kind = {item["kind"]: item for item in resources}

    assert by_kind["Namespace"]["metadata"]["name"] == "home-budget"
    assert by_kind["ServiceAccount"]["automountServiceAccountToken"] is False
    assert by_kind["Role"]["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    ]


def test_local_scripts_are_pinned_to_the_disposable_context():
    up_script = (ROOT / "scripts" / "local_logging_up.sh").read_text()
    test_script = (ROOT / "scripts" / "local_logging_test.sh").read_text()
    down_script = (ROOT / "scripts" / "local_logging_down.sh").read_text()

    assert "cluster_name=home-budget-logging" in up_script
    assert "kube_context=kind-$cluster_name" in up_script
    assert "kind-home-budget-logging" in test_script
    assert "brownrook-k3s1" not in up_script + test_script + down_script
    assert "${cluster_name}-control-plane:6443" in up_script
    assert "get secrets" in test_script
    assert "LOCAL_LOG_PROBE_" in test_script


def test_local_generated_credentials_and_certificates_are_ignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".local-logging/" in ignored
    assert "*.kubeconfig" in ignored


def test_makefile_exposes_local_logging_lifecycle():
    makefile = (ROOT / "Makefile").read_text()
    for target in (
        "dev-logging-up",
        "dev-logging-test",
        "dev-logging-status",
        "dev-logging-logs",
        "dev-logging-down",
    ):
        assert f"{target}:" in makefile


def test_compose_wrapper_supports_plugin_and_standalone_installations():
    wrapper = (ROOT / "scripts" / "docker_compose.sh").read_text()
    assert "docker compose version" in wrapper
    assert "docker-compose" in wrapper
