#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file=${1:-"$repo_root/.env.dev"}

if [ ! -f "$env_file" ]; then
  echo "Missing environment file: $env_file" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is required" >&2
  exit 2
fi

target=$(psql "$DATABASE_URL" -Atqc \
  "SELECT current_database() || '|' || COALESCE(host(inet_server_addr()), 'local')")
case "$target" in
  home_budget\|127.0.0.1|home_budget\|::1|home_budget\|local) ;;
  *)
    echo "Refusing to reset non-local database target: $target" >&2
    exit 2
    ;;
esac

stage_dir=$(mktemp -d "${TMPDIR:-/tmp}/home-budget-schema.XXXXXX")
trap 'rm -rf "$stage_dir"' EXIT HUP INT TERM

PATH="$repo_root/.venv/bin:$PATH" "$repo_root/scripts/stage_db_bootstrap.sh" "$stage_dir"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "DROP SCHEMA IF EXISTS ingest_blue CASCADE;
   DROP SCHEMA IF EXISTS ingest_green CASCADE;
   DROP SCHEMA IF EXISTS ingest CASCADE;
   DROP SCHEMA IF EXISTS ops CASCADE;
   DROP SCHEMA IF EXISTS budget CASCADE;"

for sql_file in "$stage_dir"/*.sql; do
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$sql_file"
done

echo "Rebuilt local home_budget database with the K3S bootstrap sequence."
