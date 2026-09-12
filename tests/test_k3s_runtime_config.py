import json
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import k3s_runtime_config as config
sys.path.pop(0)


def settings(**overrides):
    return {"KUBE_CONTEXT": "test-cluster", "KUBE_NAMESPACE": "home-budget",
            "HOME_BUDGET_OCR_CACHE": "/data/custom/cache", **overrides}


def test_config_map_uses_env_path_and_excludes_credentials():
    result = config.runtime_config(settings(POSTGRES_PASSWORD="secret", GHCR_TOKEN="secret"))
    assert result["data"] == {"HOME_BUDGET_OCR_CACHE": "/data/custom/cache"}
    assert result["metadata"] == {"name": "receipt-runtime-config", "namespace": "home-budget"}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("path", ["", ".ocr_cache", "/tmp/cache", "/data", "/data/../tmp/cache"])
def test_missing_or_non_persistent_path_is_rejected(path):
    with pytest.raises(ValueError, match="HOME_BUDGET_OCR_CACHE"):
        config.runtime_config(settings(HOME_BUDGET_OCR_CACHE=path))


@pytest.mark.parametrize("existing", [None, "/data/custom/cache", "/data/old/cache"])
def test_apply_creates_updates_or_reuses_config_map(existing):
    calls = []
    current = {"metadata": {"resourceVersion": "42"},
               "data": {"HOME_BUDGET_OCR_CACHE": existing, "OTHER": "preserved"}}
    worker = {"metadata": {"name": "receipt-worker"}, "spec": {"template": {"spec": {"containers": [
        {"env": [{"valueFrom": {"configMapKeyRef": {"name": "receipt-runtime-config"}}}]}]}}}}
    web = {"metadata": {"name": "ledger-web"}, "spec": {"template": {"spec": {"containers": []}}}}

    class Cluster:
        def run(self, *args, payload=None):
            calls.append(args)
            if args[0] == "get":
                return json.dumps(current) if existing is not None else ""
            if args[0] in {"create", "replace"}:
                resource = json.loads(payload)
                assert resource["data"]["HOME_BUDGET_OCR_CACHE"] == "/data/custom/cache"
                if existing is not None:
                    assert resource["metadata"]["resourceVersion"] == "42"
                    assert resource["data"]["OTHER"] == "preserved"
            return ""

        def get(self, resource):
            assert resource == "deployments"
            return {"items": [worker, web]}

        def prepare_restarts(self, names):
            assert names == ["receipt-worker"]

        def restart_deployment(self, name):
            calls.append(("argocd-restart", name))

    config.apply_config(Cluster(), config.runtime_config(settings()))
    if existing == "/data/custom/cache":
        assert len(calls) == 1
    else:
        assert [call[0] for call in calls] == ["get", "create" if existing is None else "replace", "argocd-restart"]
        assert calls[-1] == ("argocd-restart", "receipt-worker")


def test_failed_config_update_does_not_restart_workers():
    calls = []

    class Cluster:
        def run(self, *args, payload=None):
            calls.append(args[0])
            if args[0] == "get":
                return ""
            raise RuntimeError("API error")

        def get(self, resource):
            return {"items": []}

        def prepare_restarts(self, names):
            assert names == []

    with pytest.raises(RuntimeError):
        config.apply_config(Cluster(), config.runtime_config(settings()))
    assert calls == ["get", "create"]
