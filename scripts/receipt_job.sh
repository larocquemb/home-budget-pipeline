#!/usr/bin/env bash
# Run manual receipt jobs using the live cluster configuration.
set -euo pipefail

mode="${1:-}"
context="${2:-brownrook-k3s1}"
namespace="${3:-home-budget}"
node="${4:-}"
receipt="${RECEIPT:-}"
case "$mode" in
  publish|worker-test) ;;
  *) echo "Usage: $0 {publish|worker-test} [context] [namespace] [node]" >&2; exit 2 ;;
esac
if [[ -n "$receipt" ]]; then
  if [[ "$mode" != publish ]]; then
    echo "RECEIPT is supported by receipts-publish only; workers consume the shared queue." >&2
    exit 2
  fi
  if [[ "$receipt" == /* || "$receipt" =~ [[:cntrl:]] ]]; then
    echo "RECEIPT must be a path relative to the receipt inbox, without control characters." >&2
    exit 2
  fi
fi
if [[ "$mode" == worker-test && -z "$node" ]]; then
  echo "Usage: make receipts-worker-test NODE=longbow" >&2
  exit 2
fi
if [[ -n "$node" && ! "$node" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]]; then
  echo "Invalid node name: $node" >&2
  exit 2
fi
kube() { kubectl --context "$context" --namespace "$namespace" "$@"; }
if [[ -n "$node" ]]; then
  kube wait --for=condition=Ready "node/$node" --timeout=10s
fi

work_dir="$(mktemp -d)"
job="receipt-${mode}-$(date +%s)-${RANDOM}"
created=false
cleanup() {
  if [[ "$mode" == worker-test && "$created" == true ]]; then
    echo "Stopping Job/$job; the worker may finish its current receipt during its termination grace period."
    kube delete job "$job" --wait=false || true
  fi
  rm -rf "$work_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$mode" == publish ]]; then
  kube create job "$job" --from=cronjob/receipt-processor \
    --dry-run=client -o json > "$work_dir/job.json"
else
  # JSONPath emits the complete live Pod template, including secrets, mounts,
  # resources, image and telemetry configuration; values are never printed.
  template="$(kube get deployment receipt-worker -o jsonpath='{.spec.template}')"
  printf '{"apiVersion":"batch/v1","kind":"Job","metadata":{"name":"%s"},"spec":{"template":%s}}\n' \
    "$job" "$template" > "$work_dir/job.json"
fi

patch='{"spec":{"backoffLimit":0,"ttlSecondsAfterFinished":86400,"template":{"spec":{"restartPolicy":"Never"}}}}'
kubectl patch --local -f "$work_dir/job.json" --type=merge -p "$patch" \
  -o json > "$work_dir/prepared.json"
if [[ -n "$receipt" ]]; then
  # Escape the filename as a JSON string, retaining spaces and punctuation.
  escaped_receipt="${receipt//\\/\\\\}"
  escaped_receipt="${escaped_receipt//\"/\\\"}"
  kubectl patch --local -f "$work_dir/prepared.json" --type=json \
    -p "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/command\",\"value\":[\"ledger\"]},{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":[\"receipts\",\"reprocess\",\"$escaped_receipt\",\"--receipt-root\",\"/data/receipts/raw/scanned/inbox\",\"--verbose\"]}]" \
    -o json > "$work_dir/single-receipt.json"
  mv "$work_dir/single-receipt.json" "$work_dir/prepared.json"
fi
if [[ -n "$node" ]]; then
  kubectl patch --local -f "$work_dir/prepared.json" --type=merge \
    -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"kubernetes.io/hostname\":\"$node\"}}}}}" \
    -o json > "$work_dir/pinned.json"
  mv "$work_dir/pinned.json" "$work_dir/prepared.json"
fi
if [[ "$mode" == worker-test ]]; then
  kubectl patch --local -f "$work_dir/prepared.json" --type=json \
    -p '[{"op":"replace","path":"/spec/template/metadata","value":{"labels":{"app":"manual-receipt-worker"}}}]' \
    -o json > "$work_dir/worker.json"
  mv "$work_dir/worker.json" "$work_dir/prepared.json"
fi

if [[ "${RECEIPT_JOB_DRY_RUN:-0}" == 1 ]]; then
  kube create --dry-run=server -f "$work_dir/prepared.json" -o name
  echo "Validated Job/$job without creating it."
  exit 0
fi
kube create -f "$work_dir/prepared.json"
created=true
echo "Started Job/$job in $context/$namespace${node:+ on $node}."
if [[ -n "$receipt" ]]; then
  echo "Queuing one receipt for OCR refresh and extraction replacement: $receipt"
fi
if [[ "$mode" == worker-test ]]; then
  echo "Consumes real queued receipts; waits when the queue is empty. Press Ctrl+C to stop and remove this test Job."
fi
# Job creation returns before its controller necessarily creates a Pod.
# Waiting on an empty label selector fails immediately, so discover it first.
pod=""
for ((attempt = 0; attempt < 60; attempt++)); do
  pod="$(kube get pods -l "job-name=$job" -o jsonpath='{.items[*].metadata.name}')"
  [[ -n "$pod" ]] && break
  sleep 1
done
if [[ -z "$pod" ]]; then
  echo "Job/$job did not create a pod within 60 seconds." >&2
  kube describe job "$job"
  exit 1
fi
if ! kube wait --for=condition=Ready "pod/$pod" --timeout=180s; then
  # A short publisher may finish before its Ready condition is observed.
  succeeded="$(kube get job "$job" -o jsonpath='{.status.succeeded}')"
  if [[ "$succeeded" != 1 ]]; then
    kube describe pod -l "job-name=$job"
    exit 1
  fi
fi
kube get pods -l "job-name=$job" -o wide
kube logs -f "job/$job" --pod-running-timeout=180s
if [[ "$mode" == publish ]]; then
  kube wait --for=condition=Complete "job/$job" --timeout=600s
  echo "Receipt publishing completed. Job/$job will expire after 24 hours."
else
  echo "Worker exited; inspect the logs above for its outcome."
  kube wait --for=condition=Complete "job/$job" --timeout=10s
fi
