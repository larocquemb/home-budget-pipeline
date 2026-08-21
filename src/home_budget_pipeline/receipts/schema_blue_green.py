"""Blue/green lifecycle for disposable receipt-ingest database state."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psycopg

COMPONENT = "receipt_ingest"
SCHEMA_VERSION = 1
LOCK_NAME = "home-budget-receipt-backlog"
TEMPLATE_PATH = Path("/opt/app-root/src/sql/receipt_processing_template.sql")


def _ensure_state_table(conn) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS ops")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops.schema_state (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 0,
            current_schema TEXT,
            previous_schema TEXT,
            schema_hash TEXT NOT NULL DEFAULT '',
            is_dirty BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    conn.execute(
        "ALTER TABLE ops.schema_state ADD COLUMN IF NOT EXISTS previous_schema TEXT"
    )
    conn.commit()


def _state(conn):
    return conn.execute(
        """
        SELECT version, current_schema, previous_schema, schema_hash, is_dirty
          FROM ops.schema_state
         WHERE component = %s
        """,
        (COMPONENT,),
    ).fetchone()


def _mark_dirty(conn, desired_hash: str) -> None:
    conn.execute(
        """
        INSERT INTO ops.schema_state (
            component, version, current_schema, previous_schema, schema_hash, is_dirty
        )
        VALUES (%s, 0, NULL, NULL, %s, TRUE)
        ON CONFLICT (component) DO UPDATE SET
            schema_hash = EXCLUDED.schema_hash,
            is_dirty = TRUE,
            updated_at = NOW()
        """,
        (COMPONENT, desired_hash),
    )
    conn.commit()


def _build_schema(conn, target_schema: str, template: str) -> None:
    conn.execute(f'DROP SCHEMA IF EXISTS "{target_schema}" CASCADE')
    conn.execute(template.replace("__INGEST_SCHEMA__", target_schema))
    for relation in ("receipts", "receipt_processing_status"):
        exists = conn.execute(
            "SELECT to_regclass(%s)", (f"{target_schema}.{relation}",)
        ).fetchone()[0]
        if exists is None:
            raise RuntimeError(f"blue/green build missing {target_schema}.{relation}")
    conn.commit()


def _drop_budget_status_relation(conn) -> None:
    conn.execute(
        """
        DO $$
        DECLARE
            relation_kind "char";
        BEGIN
            SELECT c.relkind
              INTO relation_kind
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'budget'
               AND c.relname = 'receipt_processing_status';

            IF relation_kind = 'v' THEN
                EXECUTE 'DROP VIEW budget.receipt_processing_status CASCADE';
            ELSIF relation_kind IS NOT NULL THEN
                EXECUTE 'DROP TABLE budget.receipt_processing_status CASCADE';
            END IF;
        END;
        $$;
        """
    )


def _preserve_legacy_ingest(conn, previous_schema: str | None) -> str | None:
    if previous_schema:
        conn.execute("DROP SCHEMA IF EXISTS ingest CASCADE")
        return previous_schema

    relation_kind = conn.execute(
        """
        SELECT c.relkind
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'ingest' AND c.relname = 'receipts'
        """
    ).fetchone()
    if relation_kind and relation_kind[0] == "r":
        conn.execute("DROP SCHEMA IF EXISTS ingest_v0 CASCADE")
        conn.execute("ALTER SCHEMA ingest RENAME TO ingest_v0")
        return "ingest_v0"

    conn.execute("DROP SCHEMA IF EXISTS ingest CASCADE")
    return None


def _prune_old_versions(conn, keep: set[str]) -> None:
    schemas = conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname ~ '^ingest_v[0-9]+$'"
    ).fetchall()
    for (schema_name,) in schemas:
        if schema_name not in keep:
            conn.execute(f'DROP SCHEMA "{schema_name}" CASCADE')


def _cut_over(
    conn,
    target_schema: str,
    desired_hash: str,
    previous_schema: str | None,
) -> None:
    with conn.transaction():
        _drop_budget_status_relation(conn)
        previous_schema = _preserve_legacy_ingest(conn, previous_schema)
        conn.execute("CREATE SCHEMA ingest")
        conn.execute(
            f"CREATE VIEW ingest.receipts AS SELECT * FROM {target_schema}.receipts"
        )
        conn.execute(
            f"CREATE VIEW ingest.receipt_processing_status AS "
            f"SELECT * FROM {target_schema}.receipt_processing_status"
        )
        conn.execute(
            """
            CREATE VIEW budget.receipt_processing_status AS
            SELECT
                s.source_sha256,
                r.source_reference,
                s.status,
                s.attempts,
                s.last_error,
                s.first_attempted_at,
                s.last_attempted_at,
                s.completed_at,
                s.updated_at
            FROM ingest.receipt_processing_status s
            JOIN ingest.receipts r USING (source_sha256)
            """
        )
        conn.execute(
            """
            INSERT INTO ops.schema_state (
                component, version, current_schema, previous_schema,
                schema_hash, is_dirty, updated_at
            ) VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
            ON CONFLICT (component) DO UPDATE SET
                version = EXCLUDED.version,
                previous_schema = EXCLUDED.previous_schema,
                current_schema = EXCLUDED.current_schema,
                schema_hash = EXCLUDED.schema_hash,
                is_dirty = FALSE,
                updated_at = NOW()
            """,
            (COMPONENT, SCHEMA_VERSION, target_schema, previous_schema, desired_hash),
        )
        keep = {target_schema}
        if previous_schema:
            keep.add(previous_schema)
        _prune_old_versions(conn, keep)


def ensure_receipt_schema(conn, template_path: Path = TEMPLATE_PATH) -> bool:
    """Build/cut over only when an intentional schema version requires it."""
    template = template_path.read_text(encoding="utf-8")
    desired_hash = hashlib.sha256(template.encode("utf-8")).hexdigest()
    target_schema = f"ingest_v{SCHEMA_VERSION}"

    _ensure_state_table(conn)
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
    try:
        state = _state(conn)
        if state and state[0] == SCHEMA_VERSION:
            if state[3] == desired_hash and not state[4]:
                return False
            if state[3] != desired_hash:
                raise RuntimeError(
                    "receipt schema template changed without a SCHEMA_VERSION bump"
                )

        previous_schema = state[1] if state else None
        _mark_dirty(conn, desired_hash)
        _build_schema(conn, target_schema, template)
        _cut_over(conn, target_schema, desired_hash, previous_schema)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
        conn.commit()


def main() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("HOME_BUDGET_PG_DSN")
    if not dsn:
        raise RuntimeError("DATABASE_URL or HOME_BUDGET_PG_DSN is required")
    with psycopg.connect(dsn) as conn:
        changed = ensure_receipt_schema(conn)
    print("receipt ingest schema upgraded" if changed else "receipt ingest schema already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
