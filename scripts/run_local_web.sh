#!/usr/bin/env bash
set -euo pipefail
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
set -a
source "$repo_root/.env.dev"
set +a
if [[ -z "${LEDGER_PROXY_SECRET:-}" ]]; then
  echo "Set LEDGER_PROXY_SECRET in .env.dev" >&2
  exit 2
fi
log_dir=${HOME_BUDGET_LOCAL_LOG_DIR:-"$repo_root/.local-services"}
mkdir -p "$log_dir"
chmod 700 "$log_dir"
touch "$log_dir/web.log"
chmod 600 "$log_dir/web.log"
export LEDGER_BASE_PATH=/ledger HOST=0.0.0.0 PORT=8080
"$repo_root/.venv/bin/uvicorn" home_budget_pipeline.web.receipt_app:app \
  --host 0.0.0.0 --port 8080 --reload 2>&1 | tee -a "$log_dir/web.log"
