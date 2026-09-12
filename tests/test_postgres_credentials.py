import base64
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import pytest


SPEC = importlib.util.spec_from_file_location("postgres_credentials", Path(__file__).resolve().parents[1] / "scripts/postgres_credentials.py")
credentials = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(credentials)


def settings(**overrides):
    values = {"KUBE_CONTEXT": "test-cluster", "KUBE_NAMESPACE": "home-budget", "POSTGRES_DB": "home_budget",
              "POSTGRES_USER": "home_budget", "POSTGRES_PASSWORD": "test-only", **overrides}
    user, password, database = (quote(values[key], safe="") for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"))
    values.setdefault("DATABASE_URL", f"postgresql://{user}:{password}@postgres.example.invalid:5432/{database}")
    values.setdefault("HOME_BUDGET_PG_DSN", values["DATABASE_URL"])
    return values


def test_env_password_is_literal_and_not_executed(tmp_path):
    env = tmp_path / ".env.k3s"
    env.write_text("export POSTGRES_PASSWORD='a $HOME $(false) `false` # with spaces'\n")
    assert credentials.read_env(env)["POSTGRES_PASSWORD"] == "a $HOME $(false) `false` # with spaces"


def test_malformed_env_error_does_not_expose_password(tmp_path):
    env = tmp_path / ".env.k3s"
    env.write_text("POSTGRES_PASSWORD='sensitive-value\n")
    with pytest.raises(ValueError) as error:
        credentials.read_env(env)
    assert "sensitive-value" not in str(error.value)


def test_database_urls_use_the_configured_hostname():
    data = credentials.secret_data(settings(POSTGRES_USER="user@host", POSTGRES_PASSWORD="a:/# ?%", POSTGRES_DB="db/name"))
    url = urlsplit(data["DATABASE_URL"])
    assert url.scheme == "postgresql"
    assert url.hostname == "postgres.example.invalid" and url.port == 5432
    assert url.username == "user%40host" and url.password == "a%3A%2F%23%20%3F%25"
    assert url.path == "/db%2Fname"
    assert data["DATABASE_URL"] == data["HOME_BUDGET_PG_DSN"]
    assert data["POSTGRES_PASSWORD"] == "a:/# ?%"


def test_url_templates_expand_without_executing_or_recursively_expanding_password(tmp_path):
    env = tmp_path / ".env.k3s"
    env.write_text("\n".join([
        "POSTGRES_USER=home_budget", "POSTGRES_PASSWORD='a:/# ${UNDEFINED} $(false)'",
        "POSTGRES_DB=home_budget", "POSTGRES_HOST=custom.example", "POSTGRES_PORT=6432",
        "DATABASE_URL='postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}'",
        "HOME_BUDGET_PG_DSN='${DATABASE_URL}'",
    ]))
    values = credentials.read_env(env)
    assert values["POSTGRES_PASSWORD"] == "a:/# ${UNDEFINED} $(false)"
    url = urlsplit(values["DATABASE_URL"])
    assert url.scheme == "postgresql" and url.username == "home_budget"
    assert url.password == "a%3A%2F%23%20%24%7BUNDEFINED%7D%20%24%28false%29"
    assert url.hostname == "custom.example" and url.port == 6432
    assert url.path == "/home_budget"
    assert values["HOME_BUDGET_PG_DSN"] == values["DATABASE_URL"]


def test_missing_template_variable_is_rejected(tmp_path):
    env = tmp_path / ".env.k3s"
    env.write_text("DATABASE_URL='${MISSING}'\n")
    with pytest.raises(ValueError, match="MISSING"):
        credentials.read_env(env)


def test_mismatched_url_password_is_rejected_without_exposing_it():
    with pytest.raises(ValueError) as error:
        credentials.secret_data(settings(DATABASE_URL=settings(POSTGRES_PASSWORD="wrong-secret")["DATABASE_URL"]))
    assert "DATABASE_URL" in str(error.value)
    assert "wrong-secret" not in str(error.value)


def test_missing_urls_are_not_replaced_with_a_hardcoded_default():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        credentials.secret_data(settings(DATABASE_URL=""))


def test_blank_password_is_rejected():
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        credentials.secret_data(settings(POSTGRES_PASSWORD=""))


def test_secret_update_preserves_version_and_unrelated_keys():
    current = {"metadata": {"name": "postgres-secret", "resourceVersion": "42", "annotations": {
        "example": "keep", "kubectl.kubernetes.io/last-applied-configuration": "old-secret"}},
        "data": {"OTHER": base64.b64encode(b"keep").decode()}}
    updated = credentials.updated_secret(current, credentials.secret_data(settings()))
    assert updated["metadata"]["resourceVersion"] == "42"
    assert updated["metadata"]["annotations"] == {"example": "keep"}
    assert updated["data"]["OTHER"] == current["data"]["OTHER"]
    assert "POSTGRES_PASSWORD" not in current["data"]


@pytest.mark.parametrize("container", [
    {"env": [{"valueFrom": {"secretKeyRef": {"name": "postgres-secret"}}}]},
    {"envFrom": [{"secretRef": {"name": "postgres-secret"}}]},
])
def test_finds_database_clients(container):
    assert credentials.uses_postgres({"containers": [container]})
    assert credentials.uses_postgres({"initContainers": [container]})
    assert not credentials.uses_postgres({"containers": [{"envFrom": [{"secretRef": {"name": "rabbitmq-secret"}}]}]})


def test_apply_refuses_to_change_existing_password(tmp_path, monkeypatch):
    env = tmp_path / ".env.k3s"
    env.write_text("\n".join(f"{key}={value}" for key, value in settings().items()))
    calls = []
    def run(self, *args, payload=None):
        calls.append(args)
        return json.dumps({"data": credentials.encoded(credentials.secret_data(settings(POSTGRES_PASSWORD="old")))})
    monkeypatch.setattr(credentials.Cluster, "run", run)
    monkeypatch.setattr("sys.argv", ["postgres_credentials.py", "--env-file", str(env), "--apply"])
    with pytest.raises(SystemExit) as error:
        credentials.main()
    assert error.value.code == 1
    assert len(calls) == 1 and calls[0][0] == "get"


def test_check_does_not_contact_cluster(tmp_path, monkeypatch):
    env = tmp_path / ".env.k3s"
    env.write_text("\n".join(f"{key}={value}" for key, value in settings().items()))
    def fail(*args, **kwargs):
        raise AssertionError("Unexpected cluster access")
    monkeypatch.setattr(credentials.Cluster, "run", fail)
    monkeypatch.setattr("sys.argv", ["postgres_credentials.py", "--env-file", str(env)])
    credentials.main()


@pytest.mark.parametrize("existing_password", [None, "test-only", "old-password"])
def test_deployment_sync_creates_reuses_or_changes_password(tmp_path, monkeypatch, existing_password):
    env = tmp_path / ".env.k3s"
    env.write_text("\n".join(f"{key}={value}" for key, value in settings().items()))
    calls = []
    rotations = []

    def run(self, *args, payload=None):
        calls.append((args, payload))
        if args[:2] == ("get", "secret"):
            if existing_password is None:
                return ""
            return json.dumps({"data": credentials.encoded(
                credentials.secret_data(settings(POSTGRES_PASSWORD=existing_password)))})
        if args[:2] == ("get", "statefulset"):
            return ""
        if args[0] == "create":
            assert json.loads(payload)["data"] == credentials.encoded(credentials.secret_data(settings()))
            return "created"
        raise AssertionError(f"Unexpected cluster operation: {args[0]}")

    def rotate(cluster, current, desired):
        rotations.append(desired["POSTGRES_PASSWORD"])

    monkeypatch.setattr(credentials.Cluster, "run", run)
    monkeypatch.setattr(credentials, "rotate", rotate)
    monkeypatch.setattr("sys.argv", ["postgres_credentials.py", "--env-file", str(env), "--sync"])
    credentials.main()
    if existing_password is None:
        assert [args[0] for args, payload in calls] == ["get", "get", "create"]
        assert rotations == []
    elif existing_password == "test-only":
        assert len(calls) == 1
        assert rotations == []
    else:
        assert len(calls) == 1
        assert rotations == ["test-only"]


def test_changed_url_restarts_clients_without_changing_database_password(tmp_path, monkeypatch):
    env = tmp_path / ".env.k3s"
    desired = settings()
    env.write_text("\n".join(f"{key}={value}" for key, value in desired.items()))
    old_url = settings()["DATABASE_URL"].replace("postgres.example.invalid", "previous.example.invalid")
    current = {"data": credentials.encoded(credentials.secret_data(
        settings(DATABASE_URL=old_url, HOME_BUDGET_PG_DSN=old_url)))}
    events = []

    def run(self, *args, payload=None):
        events.append(args[0])
        if args[0] == "get":
            return json.dumps(current)
        assert args[0] == "replace"
        assert json.loads(payload)["data"] == credentials.encoded(credentials.secret_data(desired))

    def no_rotation(*args):
        raise AssertionError("A hostname change must not rotate the password")

    monkeypatch.setattr(credentials.Cluster, "run", run)
    monkeypatch.setattr(credentials, "rotate", no_rotation)
    monkeypatch.setattr(credentials.Cluster, "get", lambda self, resource: {"items": []})
    monkeypatch.setattr(credentials.Cluster, "prepare_restarts", lambda self, names: None)
    monkeypatch.setattr(credentials, "restart_clients", lambda cluster, names: events.append("restart"))
    monkeypatch.setattr("sys.argv", ["postgres_credentials.py", "--env-file", str(env), "--sync"])
    credentials.main()
    assert events == ["get", "replace", "restart"]


@pytest.mark.parametrize("api_outcome", ["success", "rejected", "committed_then_timeout"])
def test_rotation_handles_secret_write_outcomes(monkeypatch, api_outcome):
    import psycopg

    desired = credentials.secret_data(settings())
    current = {"metadata": {"resourceVersion": "42"},
               "data": credentials.encoded(credentials.secret_data(settings(POSTGRES_PASSWORD="old")))}
    events = []

    class Connection:
        pgconn = property(lambda self: self)

        def encrypt_password(self, password, user, algorithm):
            return b"new-verifier"

        def execute(self, statement, params=None):
            if isinstance(statement, str) and statement.startswith("SELECT rolpassword"):
                return self
            events.append(statement.as_string() if hasattr(statement, "as_string") else statement)
            return self

        def fetchone(self):
            return ("old-verifier",)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Cluster:
        def get(self, resource):
            if resource == "deployments":
                return {"items": [{"metadata": {"name": "ledger-web"}, "spec": {"template": {"spec": {
                    "containers": [{"envFrom": [{"secretRef": {"name": "postgres-secret"}}]}]}}}}]}
            if resource == "secret/postgres-secret":
                if api_outcome == "committed_then_timeout":
                    return credentials.updated_secret(current, desired)
                return current
            return {"items": []}

        def run(self, *args, payload=None):
            events.append(args[0])
            if args[0] == "replace" and api_outcome != "success":
                raise RuntimeError("API error")

        def prepare_restarts(self, names):
            assert names == ["ledger-web"]

        def restart_deployment(self, name):
            assert name == "ledger-web"
            events.append("argocd-restart")

    @contextmanager
    def connection(*args):
        yield Connection(), 5433

    monkeypatch.setattr(credentials, "database_connection", connection)
    def connect(**kwargs):
        assert kwargs["password"] == desired["POSTGRES_PASSWORD"]
        events.append("verify-authentication")
        return Connection()
    monkeypatch.setattr(psycopg, "connect", connect)
    if api_outcome == "rejected":
        with pytest.raises(RuntimeError, match="password restored"):
            credentials.rotate(Cluster(), current, desired)
        assert events[-1] == 'ALTER ROLE "home_budget" PASSWORD \'old-verifier\''
        assert "argocd-restart" not in events
    else:
        credentials.rotate(Cluster(), current, desired)
        assert events.index("replace") < events.index("verify-authentication") < events.index("argocd-restart")
        assert not any("old-verifier" in event for event in events)
    assert events[0] == 'ALTER ROLE "home_budget" PASSWORD \'new-verifier\''


def test_restart_is_an_argocd_action_with_configured_app_and_namespace(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setenv("ARGO_APP", "custom-ledger")
    monkeypatch.setenv("ARGO_SERVER", "argo.example")
    monkeypatch.setattr(credentials.subprocess, "run", run)
    credentials.Cluster(settings()).restart_deployment("receipt-worker")
    assert commands == [["argocd", "app", "actions", "run", "custom-ledger", "restart",
                         "--kind", "Deployment", "--group", "apps", "--namespace", "home-budget",
                         "--resource-name", "receipt-worker", "--server", "argo.example", "--grpc-web"]]


def test_restart_preflight_rejects_workloads_not_managed_by_argocd(monkeypatch):
    application = {"spec": {"destination": {"namespace": "home-budget"}}, "status": {"resources": []}}
    monkeypatch.setattr(credentials.Cluster, "argo", lambda self, *args: json.dumps(application))
    with pytest.raises(ValueError, match="managed"):
        credentials.Cluster(settings()).prepare_restarts(["receipt-worker"])
