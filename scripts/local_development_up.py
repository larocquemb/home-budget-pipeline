#!/usr/bin/env python3
"""Start authentication proxies with native macOS PostgreSQL."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from postgres_credentials import read_env
ROOT = Path(__file__).resolve().parents[1]


def validate_database_url(value: str) -> None:
    try:
        url = urlsplit(value)
        valid = (url.scheme in {"postgres", "postgresql"}
                 and url.hostname in {"127.0.0.1", "localhost", "::1"}
                 and (url.port or 5432) == 5432
                 and url.path == "/home_budget" and bool(url.username))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("DATABASE_URL must target native home_budget on localhost port 5432 with a username")


def main() -> int:
    import psycopg
    env_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".env.dev"
    if not env_file.is_absolute():
        env_file = ROOT / env_file
    if not env_file.is_file():
        raise ValueError("Copy .env.dev.example to .env.dev and fill in its values")
    values = read_env(env_file)
    database_url = values.get("DATABASE_URL", "")
    validate_database_url(database_url)
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        if conn.execute("SELECT to_regclass('budget.analytics_expenses')").fetchone()[0] is None:
            raise ValueError("Native database needs initialization; run make dev-db-reset")
        print("Native PostgreSQL:", conn.execute("SHOW server_version").fetchone()[0], flush=True)
    environment = os.environ.copy()
    environment.update(values)
    subprocess.run([str(ROOT / "scripts/docker_compose.sh"), "--env-file", str(env_file),
                    "--file", str(ROOT / "compose.dev.yaml"), "up", "--detach", "--wait"],
                   cwd=ROOT, env=environment, check=True)
    return 0


if __name__ == "__main__":
    import psycopg
    try:
        raise SystemExit(main())
    except (ValueError, subprocess.CalledProcessError, psycopg.Error) as exc:
        print(f"Local development startup failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
