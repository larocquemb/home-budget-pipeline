#!/usr/bin/env bash
# Clone the GitOps-managed template; no local Python or credentials needed.
set -euo pipefail
context="${1:-brownrook-k3s1}"
namespace="${2:-home-budget}"
job="${3:?Job name required}"
compare="${COMPARE:-0}"
collaborate="${COLLABORATE:-0}"
receipt="${RECEIPT:-}"
limit="${LIMIT:-}"
case "$compare" in 0|1) ;; *) echo 'COMPARE must be 0 or 1' >&2; exit 2 ;; esac
case "$collaborate" in 0|1) ;; *) echo 'COLLABORATE must be 0 or 1' >&2; exit 2 ;; esac
case "${PAID_FALLBACK:-0}" in 0|1) ;; *) echo 'PAID_FALLBACK must be 0 or 1' >&2; exit 2 ;; esac
if [[ "$compare" == 1 && "$collaborate" == 1 ]]; then
  echo 'Choose COMPARE=1 or COLLABORATE=1.' >&2; exit 2
fi
for setting in CONTEXT_TOKENS OUTPUT_TOKENS; do
  value="${!setting:-}"
  if [[ -n "$value" && ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "$setting must be a positive integer." >&2; exit 2
  fi
done
if [[ -n "${QWEN_MODELS:-}" && ! "$QWEN_MODELS" =~ ^[a-zA-Z0-9._:/,-]+$ ]]; then
  echo 'QWEN_MODELS must be comma-separated model names.' >&2; exit 2
fi
if [[ -z "$limit" ]]; then
  if [[ "$collaborate" == 1 ]]; then limit=1; elif [[ "$compare" == 1 ]]; then limit=10; else limit=100; fi
fi
if [[ ! "$limit" =~ ^[1-9][0-9]*$ || "$receipt" =~ [[:cntrl:]] ]]; then
  echo 'LIMIT must be positive; RECEIPT must contain no control characters.' >&2
  exit 2
fi
kube() { kubectl --context "$context" --namespace "$namespace" "$@"; }
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
kube create job "$job" --from=cronjob/product-enrichment --dry-run=client -o json > "$work_dir/job.json"
args="[\"--limit\",\"$limit\",\"--threshold\",\"0.85\",\"--write-db\""
if [[ "$compare" == 1 ]]; then args+=',"--compare-models"'; fi
if [[ "$collaborate" == 1 ]]; then args+=',"--collaborate-models"'; fi
if [[ -n "$receipt" ]]; then
  escaped_receipt="${receipt//\\/\\\\}"
  escaped_receipt="${escaped_receipt//\"/\\\"}"
  args+=",\"--receipt\",\"$escaped_receipt\""
fi
args+=']'
kubectl patch --local -f "$work_dir/job.json" --type=json \
  -p "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$args}]" \
  -o json > "$work_dir/prepared.json"
overrides=()
[[ -z "${PAID_FALLBACK:-}" ]] || overrides+=("COLLAB_PAID_FALLBACK=$PAID_FALLBACK")
[[ -z "${CONTEXT_TOKENS:-}" ]] || overrides+=("COLLAB_CONTEXT_TOKENS=$CONTEXT_TOKENS")
[[ -z "${OUTPUT_TOKENS:-}" ]] || overrides+=("COLLAB_OUTPUT_TOKENS=$OUTPUT_TOKENS")
[[ -z "${QWEN_CONTEXT_TOKENS:-}" ]] || overrides+=("QWEN_CONTEXT_TOKENS=$QWEN_CONTEXT_TOKENS")
[[ -z "${QWEN_OUTPUT_TOKENS:-}" ]] || overrides+=("QWEN_OUTPUT_TOKENS=$QWEN_OUTPUT_TOKENS")
[[ -z "${QWEN_MODELS:-}" ]] || overrides+=("OLLAMA_COLLAB_MODELS=$QWEN_MODELS")
if (( ${#overrides[@]} )); then
  kubectl set env --local -f "$work_dir/prepared.json" "${overrides[@]}" -o json > "$work_dir/overrides.json"
  mv "$work_dir/overrides.json" "$work_dir/prepared.json"
fi
if [[ "${DRY_RUN:-0}" == 1 ]]; then
  kube create --dry-run=server -f "$work_dir/prepared.json" -o name
  echo "Validated Job/$job without creating it."
  exit 0
fi
kube create -f "$work_dir/prepared.json"
echo "Started Job/$job in $context/$namespace (COMPARE=$compare COLLABORATE=$collaborate LIMIT=$limit)."
[[ "$compare" != 1 ]] || echo 'Runs OpenAI and Qwen plus Brave searches; records comparisons without changing accepted products.'
[[ "$collaborate" != 1 ]] || echo 'Shares OCR, receipt images and Brave evidence across configured models; stores proposals and review decisions without changing accepted products.'
pod=""
for ((attempt=0; attempt<60; attempt++)); do
  pod="$(kube get pods -l "job-name=$job" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -z "$pod" ]] || break
  sleep 1
done
if [[ -z "$pod" ]]; then kube describe job "$job"; exit 1; fi
# logs handles both a running Pod and one that has already completed.
kube logs -f "pod/$pod" --pod-running-timeout=180s
for ((attempt=0; attempt<30; attempt++)); do
  state="$(kube get job "$job" -o jsonpath='{.status.succeeded}/{.status.failed}')"
  if [[ "$state" == 1/* ]]; then echo "Job/$job completed."; exit 0; fi
  if [[ "$state" == */1 ]]; then echo "Job/$job failed; inspect the errors above." >&2; exit 1; fi
  sleep 1
done
echo "Job/$job has not reported completion; inspect it with kubectl." >&2
exit 1
