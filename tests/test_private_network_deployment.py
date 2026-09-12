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
    return {
        (obj["kind"], obj["metadata"]["name"]): obj
        for obj in yaml.safe_load_all(rendered)
    }


def test_mac_development_web_ports_are_loopback_only():
    compose = yaml.safe_load((ROOT / "compose.dev.yaml").read_text())
    assert compose["services"]["caddy"]["ports"] == [
        "127.0.0.1:80:80",
        "127.0.0.1:443:443",
    ]
    assert compose["services"]["caddy"]["environment"] == {
        "LEDGER_PROXY_SECRET": "${LEDGER_PROXY_SECRET:?Set LEDGER_PROXY_SECRET in .env.dev}"
    }
    caddyfile = (ROOT / "dev/Caddyfile").read_text()
    assert "X-Ledger-Proxy-Secret {$LEDGER_PROXY_SECRET}" in caddyfile


def test_private_ledger_route_uses_separate_oauth_proxy_and_entrypoint():
    resources = _render("deploy/private-lan")
    ingress = resources[("Ingress", "ledger-private")]
    proxy = resources[("Deployment", "ledger-private-oauth2-proxy")]
    assert (
        ingress["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.entrypoints"]
        == "websecure"
    )
    assert ingress["spec"]["rules"][0]["host"] == "ledger.brownrook.net"
    args = proxy["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--redirect-url=https://ledger.brownrook.net/ledger/oauth2/callback" in args
    assert "--upstream=http://ledger-web:8080" in args


def test_private_rabbitmq_route_exposes_management_only():
    resources = _render("deploy/rabbitmq-private")
    ingress = resources[("Ingress", "rabbitmq-management-private")]
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert ingress["spec"]["rules"][0]["host"] == "rabbitmq.brownrook.net"
    assert backend == {"name": "rabbitmq", "port": {"number": 15672}}
    assert resources[("Service", "rabbitmq")]["spec"]["ports"][0]["port"] == 5672
