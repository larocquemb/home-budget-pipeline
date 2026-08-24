#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
namespace=${KUBE_NAMESPACE:-home-budget}
confirm=${CONFIRM_K3S_DB_RESET:-}

if [ "$confirm" != "REBUILD" ]; then
  echo "Refusing destructive K3S database reset." >&2
  echo "Run with CONFIRM_K3S_DB_RESET=REBUILD." >&2
  exit 2
fi

command -v kubectl >/dev/null 2>&1 || {
  echo "kubectl is required" >&2
  exit 2
}

kubectl -n "$namespace" get pod postgres-0 >/dev/null

stage_dir=$(mktemp -d "${TMPDIR:-/tmp}/home-budget-k3s-schema.XXXXXX")
trap 'rm -rf "$stage_dir"' EXIT HUP INT TERM

PATH="$repo_root/.venv/bin:$PATH" "$repo_root/scripts/stage_db_bootstrap.sh" "$stage_dir"

printf '%s\n' \
  "Rebuilding PostgreSQL schemas in namespace $namespace..." \
  "This deletes budget, ingest, ingest_blue, ingest_green, and ops schema data."

kubectl -n "$namespace" exec -i postgres-0 -- sh -lc \
  'psql "$DATABASE_URL" -v ON_ERROR_STOP=1' <<'SQL'
DROP SCHEMA IF EXISTS ingest_blue CASCADE;
DROP SCHEMA IF EXISTS ingest_green CASCADE;
DROP SCHEMA IF EXISTS ingest CASCADE;
DROP SCHEMA IF EXISTS ops CASCADE;
DROP SCHEMA IF EXISTS budget CASCADE;
SQL

for sql_file in "$stage_dir"/*.sql; do
  echo "Applying $(basename "$sql_file")..."
  kubectl -n "$namespace" exec -i postgres-0 -- sh -lc \
    'psql "$DATABASE_URL" -v ON_ERROR_STOP=1' < "$sql_file"
done

kubectl -n "$namespace" exec -i postgres-0 -- sh -lc \
  'psql "$DATABASE_URL" -Atqc "SELECT to_regclass('"'"'budget.product_enrichment_cache'"'"');"' \
  | grep -qx 'budget.product_enrichment_cache' || {
    echo "Rebuild verification failed: product_enrichment_cache is missing" >&2
    exit 1
  }

echo "Rebuilt K3S home_budget database with the canonical bootstrap sequence."
