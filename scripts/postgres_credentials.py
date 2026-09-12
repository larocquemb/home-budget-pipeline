"""Provision or rotate K3s PostgreSQL credentials from an uncommitted env file."""

import argparse
import base64
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
from urllib.parse import quote, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SECRET = "postgres-secret"


def read_env(path):
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError(f"Invalid quoting at {path.name}:{number}") from None
        if tokens and tokens[0] == "export":
            tokens.pop(0)
        if not tokens:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ValueError(f"Expected KEY=value at {path.name}:{number}")
        key, value = tokens[0].split("=", 1)
        values[key] = value
    # Expand only database URL templates. Passwords and other values remain
    # literal, even when they contain shell metacharacters or ${...} text.
    for key in ("DATABASE_URL", "HOME_BUDGET_PG_DSN"):
        if key not in values:
            continue

        def substitute(match):
            name = match.group(1)
            allowed = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST", "POSTGRES_PORT"}
            if key == "HOME_BUDGET_PG_DSN":
                allowed.add("DATABASE_URL")
            if name not in allowed or not values.get(name):
                raise ValueError(f"Set a supported variable {name} referenced by {key}")
            value = values[name]
            return quote(value, safe="") if name in {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} else value

        values[key] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, values[key])
    return values


def secret_data(values):
    required = ("KUBE_CONTEXT", "KUBE_NAMESPACE", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
                "DATABASE_URL", "HOME_BUDGET_PG_DSN")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError("Set " + ", ".join(missing) + " in .env.k3s")
    if any("\x00" in values[key] or "\n" in values[key] or "\r" in values[key] for key in required):
        raise ValueError("Deployment values must be single-line strings without NUL characters")
    for key in ("DATABASE_URL", "HOME_BUDGET_PG_DSN"):
        try:
            url = urlsplit(values[key])
            valid = (url.scheme in {"postgres", "postgresql"} and url.hostname
                     and (url.port is None or 1 <= url.port <= 65535)
                     and not url.fragment
                     and unquote(url.username or "") == values["POSTGRES_USER"]
                     and unquote(url.password or "") == values["POSTGRES_PASSWORD"]
                     and unquote(url.path.removeprefix("/")) == values["POSTGRES_DB"])
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{key} must be a PostgreSQL URL matching POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB")
    return {key: values[key] for key in required if key not in {"KUBE_CONTEXT", "KUBE_NAMESPACE"}}


def encoded(data):
    return {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}


def updated_secret(current, data):
    result = copy.deepcopy(current)
    result.setdefault("data", {}).update(encoded(data))
    # Avoid retaining a second copy of an obsolete password in apply annotations.
    result.get("metadata", {}).get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return result


def uses_postgres(pod_spec):
    for container in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
        if any(env.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == SECRET
               for env in container.get("env", [])):
            return True
        if any(env.get("secretRef", {}).get("name") == SECRET for env in container.get("envFrom", [])):
            return True
    return False


class Cluster:
    def __init__(self, values):
        self.command = ["kubectl", "--context", values["KUBE_CONTEXT"], "-n", values["KUBE_NAMESPACE"]]
        self.namespace = values["KUBE_NAMESPACE"]
        self.argo_app = os.environ.get("ARGO_APP", "ledger")
        self.argo_server = os.environ.get("ARGO_SERVER", "argocd.brownrook.net")

    def run(self, *args, payload=None):
        result = subprocess.run(self.command + list(args), input=payload, text=True,
                                capture_output=True, timeout=60)
        if result.returncode:
            # API errors can include a submitted Secret, so never echo stderr.
            raise RuntimeError(f"kubectl {args[0]} failed; credentials and command output withheld")
        return result.stdout

    def get(self, resource):
        return json.loads(self.run("get", resource, "-o", "json"))

    def argo(self, *args):
        result = subprocess.run(["argocd", "app", *args, "--server", self.argo_server, "--grpc-web"],
                                text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("Argo CD operation failed; check the application and Argo CD login")
        return result.stdout

    def prepare_restarts(self, names):
        if not names:
            return
        application = json.loads(self.argo("get", self.argo_app, "-o", "json"))
        if application.get("spec", {}).get("destination", {}).get("namespace") != self.namespace:
            raise ValueError("Argo CD application destination does not match KUBE_NAMESPACE")
        managed = {item["name"] for item in application.get("status", {}).get("resources", [])
                   if item.get("kind") == "Deployment" and item.get("group") == "apps"
                   and item.get("namespace", self.namespace) == self.namespace}
        if set(names) - managed:
            raise ValueError("Database/OCR client Deployments must be managed by the configured Argo CD application")

    def restart_deployment(self, name):
        self.argo("actions", "run", self.argo_app, "restart", "--kind", "Deployment", "--group", "apps",
                  "--namespace", self.namespace, "--resource-name", name)
        print(f"Argo CD restart requested: deployment/{name}", flush=True)


def restart_clients(cluster, names=None):
    if names is None:
        names = [item["metadata"]["name"] for item in cluster.get("deployments")["items"]
                 if uses_postgres(item["spec"]["template"]["spec"])]
    for name in names:
        cluster.restart_deployment(name)


@contextmanager
def database_connection(cluster, credentials):
    import psycopg

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    forward = subprocess.Popen(
        cluster.command + ["port-forward", "svc/postgres", f"{port}:5432", "--address", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    connection = None
    try:
        for _ in range(30):
            if forward.poll() is not None:
                break
            try:
                connection = psycopg.connect(host="127.0.0.1", port=port,
                                             user=credentials["POSTGRES_USER"],
                                             password=credentials["POSTGRES_PASSWORD"],
                                             dbname=credentials["POSTGRES_DB"], connect_timeout=2,
                                             autocommit=True)
                break
            except psycopg.OperationalError:
                time.sleep(0.2)
        if connection is None:
            raise RuntimeError("Could not authenticate through the PostgreSQL port-forward")
        with connection:
            yield connection, port
    finally:
        forward.terminate()
        try:
            forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forward.kill()
            forward.wait()


def rotate(cluster, current, desired):
    import psycopg
    from psycopg import sql

    old = {key: base64.b64decode(current["data"][key]).decode()
           for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")}
    if any(old[key] != desired[key] for key in ("POSTGRES_USER", "POSTGRES_DB")):
        raise ValueError("Password rotation cannot rename the existing database or user")
    for item in cluster.get("cronjobs")["items"]:
        if uses_postgres(item["spec"]["jobTemplate"]["spec"]["template"]["spec"]) and not item["spec"].get("suspend"):
            raise ValueError(f"Suspend CronJob {item['metadata']['name']} before rotating")
    for item in cluster.get("jobs")["items"]:
        finished = any(c["type"] in ("Complete", "Failed") and c["status"] == "True"
                       for c in item.get("status", {}).get("conditions", []))
        if not finished and uses_postgres(item["spec"]["template"]["spec"]):
            raise ValueError(f"Wait for Job {item['metadata']['name']} to finish before rotating")
    clients = [item["metadata"]["name"] for item in cluster.get("deployments")["items"]
               if uses_postgres(item["spec"]["template"]["spec"])]
    cluster.prepare_restarts(clients)
    replacement = updated_secret(current, desired)
    with database_connection(cluster, old) as (connection, port):
        old_verifier = connection.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                                          (old["POSTGRES_USER"],)).fetchone()[0]
        verifier = connection.pgconn.encrypt_password(desired["POSTGRES_PASSWORD"].encode(),
                                                      old["POSTGRES_USER"].encode(), b"scram-sha-256").decode()
        statement = sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(old["POSTGRES_USER"]), sql.Literal(verifier))
        connection.execute(statement)
        try:
            cluster.run("replace", "-f", "-", payload=json.dumps(replacement))
        except Exception:
            # Read back after an ambiguous API error before deciding to roll back.
            try:
                actual = cluster.get(f"secret/{SECRET}")
            except Exception:
                raise RuntimeError("Database password changed, but Secret update could not be confirmed. "
                                   "Check postgres-secret against .env.k3s before retrying.") from None
            if any(actual.get("data", {}).get(key) != value for key, value in encoded(desired).items()):
                connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(old["POSTGRES_USER"]), sql.Literal(old_verifier)))
                raise RuntimeError("Secret update failed; database password restored") from None
        with psycopg.connect(host="127.0.0.1", port=port, user=desired["POSTGRES_USER"],
                             password=desired["POSTGRES_PASSWORD"], dbname=desired["POSTGRES_DB"],
                             connect_timeout=10) as check:
            check.execute("SELECT 1").fetchone()
    print("Database password and postgres-secret updated; new password authentication verified.", flush=True)
    restart_clients(cluster, clients)
    print("CronJobs retain their suspend settings. Check client rollout status before resuming them.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.k3s")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="Create or refresh the Secret without changing an existing password")
    action.add_argument("--rotate", action="store_true", help="Change the live password and Secret, then restart database clients")
    action.add_argument("--sync", action="store_true", help="Use the env-file password for deployment; change it only when different")
    args = parser.parse_args()
    try:
        values = read_env(args.env_file)
        desired = secret_data(values)
        if not args.apply and not args.rotate and not args.sync:
            print(f"PostgreSQL settings valid for {values['KUBE_CONTEXT']}/{values['KUBE_NAMESPACE']}; no cluster changes.")
            return
        cluster = Cluster(values)
        raw = cluster.run("get", "secret", SECRET, "--ignore-not-found", "-o", "json")
        current = json.loads(raw) if raw.strip() else None
        if args.rotate:
            if current is None:
                raise ValueError("No existing postgres-secret; use --apply for a new deployment")
            rotate(cluster, current, desired)
        elif current is None:
            # A missing Secret on an existing StatefulSet is not a fresh deployment.
            if cluster.run("get", "statefulset", "postgres", "--ignore-not-found", "-o", "name").strip():
                raise ValueError("PostgreSQL already exists; recover its current Secret before changing credentials")
            resource = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": SECRET, "namespace": values["KUBE_NAMESPACE"]}, "data": encoded(desired)}
            cluster.run("create", "-f", "-", payload=json.dumps(resource))
            print("Created postgres-secret for initial deployment.")
        else:
            changed = any(current["data"].get(key) != encoded(desired)[key]
                          for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"))
            if changed:
                if not args.sync:
                    raise ValueError("Existing credentials differ; use --rotate to change the running database password")
                rotate(cluster, current, desired)
            elif all(current["data"].get(key) == value for key, value in encoded(desired).items()):
                print("PostgreSQL already uses the specified deployment password; no credential changes or restarts.")
            else:
                clients = [item["metadata"]["name"] for item in cluster.get("deployments")["items"]
                           if uses_postgres(item["spec"]["template"]["spec"])]
                cluster.prepare_restarts(clients)
                cluster.run("replace", "-f", "-", payload=json.dumps(updated_secret(current, desired)))
                print("Refreshed postgres-secret; database credentials unchanged.")
                restart_clients(cluster, clients)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Do not emit raw subprocess inputs or database error text containing secrets.
        if type(exc) in (ValueError, RuntimeError):
            parser.exit(1, f"{exc}\n")
        parser.exit(1, "Credential operation failed; check connectivity and local file access.\n")
    except Exception:
        parser.exit(1, "Database operation failed; credential details withheld. Check database and Secret consistency before retrying.\n")


if __name__ == "__main__":
    main()
