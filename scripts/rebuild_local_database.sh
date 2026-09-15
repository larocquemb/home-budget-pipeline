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

client_target=$(python3 - <<'PY'
import os
from urllib.parse import unquote, urlsplit

url = urlsplit(os.environ["DATABASE_URL"])
print(f"{unquote(url.path.removeprefix('/'))}|{url.hostname or ''}|{url.port or 5432}")
PY
)
case "$client_target" in
  home_budget\|127.0.0.1\|5433|home_budget\|localhost\|5433|home_budget\|::1\|5433) ;;
  *)
    echo "Refusing to reset non-local database target: $client_target" >&2
    exit 2
    ;;
esac

database=$(psql "$DATABASE_URL" -Atqc "SELECT current_database()")
if [ "$database" != "home_budget" ]; then
  echo "Refusing to reset unexpected database: $database" >&2
  exit 2
fi

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
