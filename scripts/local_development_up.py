#!/usr/bin/env python3
"""Start local authentication against the isolated Arsene development backends."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
from postgres_credentials import read_env
from home_budget_pipeline.development_backend import validate_database_url, validate_endpoints, verify_database
ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    import psycopg
    env_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".env.dev"
    if not env_file.is_absolute():
        env_file = ROOT / env_file
    if not env_file.is_file():
        raise ValueError("Copy .env.dev.example to .env.dev and fill in its values")
    values = read_env(env_file)
    database_url = values.get("DATABASE_URL", "")
    validate_endpoints(values)
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        verify_database(conn)
        if conn.execute("SELECT to_regclass('budget.analytics_expenses')").fetchone()[0] is None:
            raise ValueError("Development database needs initialization; run ledger development bootstrap")
        print("Arsene development PostgreSQL:", conn.execute("SHOW server_version").fetchone()[0], flush=True)
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
