#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
case "${1:-}" in
  worker|worker-[0-9]*) mode=consume ;;
  collector) mode=collect ;;
  *) echo "Usage: run_receipt_service.sh worker|collector" >&2; exit 2 ;;
esac

set -a
. "$repo_root/.env.dev"
set +a
export PYTHONUNBUFFERED=1
if [ "$mode" = consume ]; then
  if [ -n "${HOME_BUDGET_SERVICE_OCR_THREADS:-}" ]; then
    export HOME_BUDGET_OCR_THREADS="$HOME_BUDGET_SERVICE_OCR_THREADS"
  fi
  if [ -n "${HOME_BUDGET_OCR_THREADS:-}" ]; then
    export OMP_NUM_THREADS=1
    export OMP_THREAD_LIMIT="$HOME_BUDGET_OCR_THREADS"
    export OPENBLAS_NUM_THREADS="$HOME_BUDGET_OCR_THREADS"
    export MKL_NUM_THREADS="$HOME_BUDGET_OCR_THREADS"
  fi
fi
case "$mode" in
  consume) export OTEL_SERVICE_NAME=home-budget-local-receipt-worker ;;
  collect) export OTEL_SERVICE_NAME=home-budget-local-ocr-collector ;;
esac
exec "$repo_root/.venv/bin/ledger" receipts "$mode"
