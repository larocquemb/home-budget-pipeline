#!/usr/bin/env python3
"""Start the Colima development stack with a cleanly isolated PostgreSQL 18."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit

from postgres_credentials import read_env


ROOT = Path(__file__).resolve().parents[1]


def parse_database_url(database_url: str, *, require_password: bool) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(database_url)
        port = parsed.port or 5432
    except ValueError as exc:
        raise ValueError("DATABASE_URL is not a valid PostgreSQL URL") from exc

    database = unquote(parsed.path.removeprefix("/"))
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port not in {5432, 5433}
        or database != "home_budget"
        or not username
        or (require_password and not password)
    ):
        raise ValueError(
            "DATABASE_URL must target home_budget on local port 5432 or 5433 and include a username"
        )

    return username, password, database


def runtime_database_url(source_url: str, credentials_file: Path) -> str:
    requested_user, _, requested_database = parse_database_url(source_url, require_password=False)
    if credentials_file.is_file():
        runtime_url = read_env(credentials_file).get("DATABASE_URL", "")
        runtime_user, _, runtime_database = parse_database_url(runtime_url, require_password=True)
        if runtime_user != requested_user or runtime_database != requested_database:
            raise ValueError(
                "The saved local PostgreSQL 18 identity differs from .env.dev; "
                "remove the local PostgreSQL 18 volume and .local-postgres to change it"
            )
        return runtime_url

    password = secrets.token_urlsafe(32)
    runtime_url = (
        f"postgresql://{quote(requested_user, safe='')}:{quote(password, safe='')}"
        f"@127.0.0.1:5433/{quote(requested_database, safe='')}"
    )
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    credentials_file.write_text(f"DATABASE_URL={shlex.quote(runtime_url)}\n")
    credentials_file.chmod(0o600)
    return runtime_url


def database_settings(database_url: str) -> dict[str, str]:
    username, password, database = parse_database_url(database_url, require_password=True)

    return {
        "POSTGRES_USER": username,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": database,
    }


def run(*args: str, environment: dict[str, str]) -> None:
    subprocess.run(args, cwd=ROOT, env=environment, check=True)


def main() -> int:
    env_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".env.dev"
    if not env_file.is_absolute():
        env_file = ROOT / env_file
    if not env_file.is_file():
        raise ValueError(f"Missing environment file: {env_file}")

    values = read_env(env_file)
    source_database_url = values.get("DATABASE_URL", "")
    if not source_database_url:
        raise ValueError("DATABASE_URL is required in .env.dev")

    local_dir = ROOT / ".local-postgres"
    database_url = runtime_database_url(source_database_url, local_dir / "runtime.env")

    environment = os.environ.copy()
    environment.update(values)
    environment["DATABASE_URL"] = database_url
    environment.update(database_settings(database_url))
    environment.setdefault("POSTGRES_OAUTH_AUDIENCE", "local-development")
    environment["PATH"] = f"{ROOT / '.venv' / 'bin'}:{environment.get('PATH', '')}"

    init_dir = local_dir / "init"
    shutil.rmtree(init_dir, ignore_errors=True)
    init_dir.mkdir(parents=True)
    run(str(ROOT / "scripts" / "stage_db_bootstrap.sh"), str(init_dir), environment=environment)

    compose = str(ROOT / "scripts" / "docker_compose.sh")
    compose_args = (compose, "--env-file", str(env_file), "--file", str(ROOT / "compose.dev.yaml"))
    run(*compose_args, "up", "--detach", "--wait", environment=environment)
    run(
        *compose_args,
        "exec",
        "--no-TTY",
        "postgres",
        "sh",
        "-ec",
        "psql --username \"$POSTGRES_USER\" --dbname \"$POSTGRES_DB\" "
        "--tuples-only --no-align --command \"SELECT current_setting('server_version'), "
        "current_setting('oauth_validator_libraries');\"",
        environment=environment,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"Local development startup failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
