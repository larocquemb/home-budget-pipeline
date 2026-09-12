import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_queue_overlay_renders_publisher_workers_and_persistent_broker():
    if not shutil.which("kubectl"):
        pytest.skip("kubectl is not installed")
    rendered = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "deploy/rabbitmq")], text=True)
    resources = {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(rendered)}
    publisher = resources["CronJob", "receipt-processor"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    worker_spec = resources["Deployment", "receipt-worker"]["spec"]["template"]["spec"]
    worker = worker_spec["containers"][0]
    assert resources["Deployment", "receipt-worker"]["spec"]["strategy"] == {
        "type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }
    assert publisher["command"] == worker["command"] == ["ledger"]
    assert publisher["args"] == ["receipts", "publish", "/data/receipts/raw/scanned/inbox"]
    assert worker["args"] == ["receipts", "consume", "/data/receipts/raw/scanned/inbox"]
    assert publisher["image"] == worker["image"]
    assert "latest" not in worker["image"]
    assert worker_spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == "home-budget-data"
    assert worker_spec["terminationGracePeriodSeconds"] == 900
    for container in (publisher, worker):
        env = {entry["name"]: entry for entry in container["env"]}
        assert env["RABBITMQ_URL"]["valueFrom"]["secretKeyRef"]["name"] == "rabbitmq-secret"
        assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "postgres-secret"
        assert env["HOME_BUDGET_OCR_CACHE"]["valueFrom"]["configMapKeyRef"] == {
            "name": "receipt-runtime-config", "key": "HOME_BUDGET_OCR_CACHE",
        }
    broker = resources["StatefulSet", "rabbitmq"]["spec"]
    assert broker["replicas"] == 1
    assert broker["volumeClaimTemplates"][0]["metadata"]["name"] == "rabbitmq-data"
    assert ("Secret", "rabbitmq-secret") not in resources
    assert ("ConfigMap", "receipt-runtime-config") not in resources


def test_default_deployment_remains_local_and_ci_exercises_live_broker():
    base = (ROOT / "k8s/kustomization.yaml").read_text()
    assert "rabbitmq" not in base
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    integration = workflow["jobs"]["postgres-integration"]
    assert "rabbitmq" in integration["services"]
    step = next(step for step in integration["steps"] if "TEST_RABBITMQ_URL" in step.get("env", {}))
    assert "-m integration" in step["run"]
