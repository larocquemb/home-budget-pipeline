#!/usr/bin/env bash
set -euo pipefail
context="${1:-brownrook-k3s1}"
namespace="${2:-home-budget}"
job="receipt-graph-manual-$(date +%s)-$RANDOM"
kube() { kubectl --context "$context" -n "$namespace" "$@"; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
kube create job "$job" --from=cronjob/receipt-graph --dry-run=client -o json > "$work/job.json"
if [[ -n "${SHA:-}" ]]; then
  [[ "$SHA" =~ ^[0-9a-f]{64}$ ]] || { echo 'SHA must be a lowercase receipt SHA-256.' >&2; exit 2; }
  kubectl patch --local -f "$work/job.json" --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":[\"--sha\",\"$SHA\"]}]" -o json > "$work/prepared.json"
else
  cp "$work/job.json" "$work/prepared.json"
fi
if [[ "${DRY_RUN:-0}" == 1 ]]; then
  kube create --dry-run=server -f "$work/prepared.json" -o name
  exit 0
fi
kube create -f "$work/prepared.json"
for ((i=0; i<60; i++)); do
  pod="$(kube get pods -l "job-name=$job" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -z "$pod" ]] || break
  sleep 1
done
[[ -n "${pod:-}" ]] || { kube describe job "$job"; exit 1; }
kube logs -f "$pod" --pod-running-timeout=180s
kube wait --for=condition=complete "job/$job" --timeout=30s
