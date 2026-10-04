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
    worker_deployment = resources["Deployment", "receipt-worker"]["spec"]
    collector_spec = resources["Deployment", "ocr-results-collector"]["spec"]
    collector = collector_spec["template"]["spec"]["containers"][0]
    assert worker_deployment["strategy"] == {
        "type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }
    assert "replicas" not in worker_deployment  # KEDA/HPA owns this field, not Argo CD.
    assert publisher["command"] == worker["command"] == ["ledger"]
    assert publisher["args"] == ["receipts", "publish", "/data/receipts/raw/scanned/inbox"]
    assert worker["args"] == ["receipts", "consume", "/data/receipts/raw/scanned/inbox"]
    assert publisher["image"] == worker["image"] == collector["image"]
    assert "latest" not in worker["image"]
    assert worker["resources"] == {
        "requests": {"cpu": "1", "memory": "8Gi"},
        "limits": {"cpu": "6", "memory": "8Gi"},
    }
    assert worker_spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"] == {
        "nodeSelectorTerms": [{"matchExpressions": [{
            "key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist",
        }]}],
    }
    assert worker_spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == "home-budget-data"
    assert worker_spec["terminationGracePeriodSeconds"] == 900
    for container in (publisher, worker, collector):
        env = {entry["name"]: entry for entry in container["env"]}
        assert env["RABBITMQ_URL"]["valueFrom"]["secretKeyRef"]["name"] == "rabbitmq-secret"
    publisher_env = {entry["name"]: entry for entry in publisher["env"]}
    worker_env = {entry["name"]: entry for entry in worker["env"]}
    assert worker_env["K8S_NODE_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    collector_env = {entry["name"]: entry for entry in collector["env"]}
    assert publisher_env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "postgres-secret"
    assert "DATABASE_URL" not in worker_env
    assert worker["envFrom"] == [
        {"configMapRef": {"name": "ocr-artifact-storage", "optional": True}},
        {"secretRef": {"name": "ocr-artifact-storage", "optional": True}},
    ]
    assert collector_env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "postgres-secret"
    assert collector["args"] == ["receipts", "collect"]
    assert collector_spec["replicas"] == 2
    scaler = resources["ScaledObject", "receipt-worker"]["spec"]
    assert scaler["scaleTargetRef"] == {
        "name": "receipt-worker",
        "envSourceContainerName": "receipt-worker",
    }
    assert (scaler["minReplicaCount"], scaler["maxReplicaCount"]) == (1, 3)
    assert worker_spec["nodeSelector"] == {"kubernetes.io/hostname": "arsene"}
    longbow_deployment = resources["Deployment", "receipt-worker-longbow"]["spec"]
    longbow_spec = longbow_deployment["template"]["spec"]
    longbow_worker = longbow_spec["containers"][0]
    assert longbow_deployment["replicas"] == 1
    assert longbow_spec["nodeSelector"] == {"kubernetes.io/hostname": "longbow"}
    assert longbow_worker["resources"] == {
        "requests": {"cpu": "1", "memory": "8Gi"},
        "limits": {"cpu": "2", "memory": "8Gi"},
    }
    assert {key: value for key, value in longbow_worker.items() if key != "resources"} == {
        key: value for key, value in worker.items() if key != "resources"
    }
    assert longbow_spec["volumes"] == worker_spec["volumes"]
    for deployment in (worker_deployment, longbow_deployment):
        assert deployment["template"]["metadata"]["labels"]["workload"] == "receipt-worker"
    # Preserve the existing immutable selector, and keep the new pool disjoint.
    assert worker_deployment["selector"] == {"matchLabels": {"app": "receipt-worker"}}
    assert longbow_deployment["selector"] == {"matchLabels": {"app": "receipt-worker-longbow"}}
    assert scaler["cooldownPeriod"] == 600
    assert scaler["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]["scaleDown"] == {
        "stabilizationWindowSeconds": 600,
        "selectPolicy": "Min",
        "policies": [{"type": "Pods", "value": 1, "periodSeconds": 300}],
    }
    trigger = scaler["triggers"][0]
    assert trigger == {
        "type": "rabbitmq",
        "metadata": {
            "hostFromEnv": "RABBITMQ_URL",
            "protocol": "amqp",
            "queueName": "receipts.v1.work",
            "mode": "QueueLength",
            "value": "1",
            "activationValue": "0",
        },
    }
    for container in (publisher, worker):
        env = {entry["name"]: entry for entry in container["env"]}
        assert env["HOME_BUDGET_OCR_CACHE"]["valueFrom"]["configMapKeyRef"] == {
            "name": "receipt-runtime-config", "key": "HOME_BUDGET_OCR_CACHE",
        }
    broker = resources["StatefulSet", "rabbitmq"]["spec"]
    assert broker["replicas"] == 1
    assert broker["volumeClaimTemplates"][0]["metadata"]["name"] == "rabbitmq-data"
    broker_container = broker["template"]["spec"]["containers"][0]
    assert {port["containerPort"] for port in broker_container["ports"]} >= {
        5672, 15672, 15692,
    }
    service_ports = {
        port["name"]: port["port"]
        for port in resources["Service", "rabbitmq"]["spec"]["ports"]
    }
    assert service_ports["prometheus"] == 15692
    rabbit_config = resources["ConfigMap", "rabbitmq-config"]["data"]
    assert "rabbitmq_prometheus" in rabbit_config["enabled_plugins"]
    assert any(
        mount["mountPath"] == "/etc/rabbitmq/enabled_plugins"
        and mount["subPath"] == "enabled_plugins"
        for mount in broker_container["volumeMounts"]
    )
    assert ("Secret", "rabbitmq-secret") not in resources
    assert ("ConfigMap", "receipt-runtime-config") not in resources


@pytest.mark.parametrize("overlay", [
    "rabbitmq", "rabbitmq-private", "rabbitmq-private-logging", "rabbitmq-private-telemetry",
])
def test_worker_pools_share_configuration_and_immutable_images(overlay):
    if not shutil.which("kubectl"):
        pytest.skip("kubectl is not installed")
    rendered = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "deploy" / overlay)], text=True)
    resources = {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(rendered)}
    arsene = resources["Deployment", "receipt-worker"]["spec"]["template"]["spec"]
    longbow = resources["Deployment", "receipt-worker-longbow"]["spec"]["template"]["spec"]
    publisher = resources["CronJob", "receipt-processor"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert len(arsene["containers"]) == len(longbow["containers"]) == 1
    arsene_worker, longbow_worker = arsene["containers"][0], longbow["containers"][0]
    assert {key: value for key, value in arsene_worker.items() if key != "resources"} == {
        key: value for key, value in longbow_worker.items() if key != "resources"
    }
    assert arsene_worker["resources"]["limits"]["cpu"] == "6"
    assert longbow_worker["resources"]["limits"]["cpu"] == "2"
    assert arsene["volumes"] == longbow["volumes"]
    assert longbow["containers"][0]["image"] == publisher["image"]
    if overlay == "rabbitmq-private-telemetry":
        env = {item["name"]: item for item in longbow["containers"][0]["env"]}
        assert env["HOME_BUDGET_TELEMETRY_ENABLED"]["value"] == "true"
        assert env["K8S_NODE_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
        assert any(volume.get("secret", {}).get("secretName") == "receipt-telemetry-client-tls"
                   for volume in longbow["volumes"])


def test_rabbitmq_secret_example_uses_cross_namespace_service_dns():
    secret = yaml.safe_load((ROOT / "k8s/rabbitmq-secret.example.yaml").read_text())
    assert secret["stringData"]["RABBITMQ_URL"] == (
        "amqp://receipts:REPLACE_ME@"
        "rabbitmq.home-budget.svc.cluster.local:5672/receipts"
    )


def test_default_deployment_remains_local_and_ci_exercises_live_broker():
    base = (ROOT / "k8s/kustomization.yaml").read_text()
    assert "rabbitmq" not in base
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    integration = workflow["jobs"]["postgres-integration"]
    assert "rabbitmq" in integration["services"]
    step = next(step for step in integration["steps"] if "TEST_RABBITMQ_URL" in step.get("env", {}))
    assert "-m integration" in step["run"]
