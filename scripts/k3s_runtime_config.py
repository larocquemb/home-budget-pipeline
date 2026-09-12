"""Configure K3s receipt-processing settings from .env.k3s."""

import argparse
import copy
import json
from pathlib import Path, PurePosixPath
import subprocess

from postgres_credentials import Cluster, ROOT, read_env


CONFIG_MAP = "receipt-runtime-config"
CACHE_KEY = "HOME_BUDGET_OCR_CACHE"


def runtime_config(values):
    missing = [key for key in ("KUBE_CONTEXT", "KUBE_NAMESPACE", CACHE_KEY) if not values.get(key)]
    if missing:
        raise ValueError("Set " + ", ".join(missing) + " in .env.k3s")
    cache = values[CACHE_KEY]
    path = PurePosixPath(cache)
    if (not path.is_absolute() or not path.is_relative_to("/data") or path == PurePosixPath("/data")
            or ".." in path.parts or any(char in cache for char in "\n\r\x00")):
        raise ValueError("HOME_BUDGET_OCR_CACHE must be a directory below the pod's /data PV mount")
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": CONFIG_MAP, "namespace": values["KUBE_NAMESPACE"]},
        "data": {CACHE_KEY: cache},
    }


def uses_runtime_config(pod_spec):
    for container in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
        if any(env.get("valueFrom", {}).get("configMapKeyRef", {}).get("name") == CONFIG_MAP
               for env in container.get("env", [])):
            return True
        if any(env.get("configMapRef", {}).get("name") == CONFIG_MAP for env in container.get("envFrom", [])):
            return True
    return False


def apply_config(cluster, desired):
    raw = cluster.run("get", "configmap", CONFIG_MAP, "--ignore-not-found", "-o", "json")
    current = json.loads(raw) if raw.strip() else None
    if current is not None and all(current.get("data", {}).get(key) == value for key, value in desired["data"].items()):
        print("OCR cache configuration already matches .env.k3s; no changes or restarts.")
        return
    clients = [item["metadata"]["name"] for item in cluster.get("deployments")["items"]
               if uses_runtime_config(item["spec"]["template"]["spec"])]
    cluster.prepare_restarts(clients)
    if current is None:
        cluster.run("create", "-f", "-", payload=json.dumps(desired))
    else:
        updated = copy.deepcopy(current)
        updated.setdefault("data", {}).update(desired["data"])
        cluster.run("replace", "-f", "-", payload=json.dumps(updated))
    print(f"Configured {CONFIG_MAP}: {CACHE_KEY}={desired['data'][CACHE_KEY]}", flush=True)
    for name in clients:
        cluster.restart_deployment(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.k3s")
    parser.add_argument("--apply", action="store_true", help="Update the ConfigMap and restart its Deployment clients")
    args = parser.parse_args()
    try:
        values = read_env(args.env_file)
        desired = runtime_config(values)
        if args.apply:
            apply_config(Cluster(values), desired)
        else:
            print(f"K3s OCR cache configuration valid: {desired['data'][CACHE_KEY]}; no cluster changes.")
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    except (OSError, subprocess.SubprocessError):
        parser.exit(1, "Runtime configuration failed; check connectivity and local file access.\n")


if __name__ == "__main__":
    main()
