#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
kube_context=kind-home-budget-logging
namespace=home-budget
compose_file="$root_dir/deploy/local-logging/compose.yaml"
compose="$root_dir/scripts/docker_compose.sh"
ca_file="$root_dir/.local-logging/tls/ca.crt"
probe="LOCAL_LOG_PROBE_$(date +%s)"
probe_pod="local-log-client-$(date +%s)"

ready=0
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error \
    --cacert "$ca_file" \
    https://localhost:13100/ready >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if test "$ready" -ne 1; then
  echo "Local Loki did not become ready over verified TLS." >&2
  exit 1
fi

ready=0
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error \
    http://localhost:12345/-/ready >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if test "$ready" -ne 1; then
  echo "Local Alloy did not become ready." >&2
  exit 1
fi

kubectl --context "$kube_context" -n "$namespace" rollout status \
  deployment/home-budget-local --timeout=120s

reader="system:serviceaccount:${namespace}:external-log-reader"
kubectl --context "$kube_context" auth can-i \
  --as="$reader" list pods -n "$namespace" | grep -qx yes
kubectl --context "$kube_context" auth can-i \
  --as="$reader" get pods/log -n "$namespace" | grep -qx yes
if kubectl --context "$kube_context" auth can-i \
  --as="$reader" get secrets -n "$namespace" | grep -qx yes; then
  echo "RBAC failure: the external log reader can access Secrets." >&2
  exit 1
fi

kubectl --context "$kube_context" -n "$namespace" run "$probe_pod" \
  --image=curlimages/curl:8.12.1 \
  --restart=Never \
  --command -- \
  curl --fail --silent --show-error \
    "http://home-budget-local:8080/ledger/health?probe=$probe"

kubectl --context "$kube_context" -n "$namespace" wait \
  --for=jsonpath='{.status.phase}'=Succeeded \
  "pod/$probe_pod" --timeout=120s
kubectl --context "$kube_context" -n "$namespace" delete \
  "pod/$probe_pod" --wait=false >/dev/null

if ! kubectl --context "$kube_context" -n "$namespace" logs \
  deployment/home-budget-local --tail=100 | grep -Fq "$probe"; then
  echo "The home-budget Pod did not write the expected access-log probe." >&2
  exit 1
fi

query="{cluster=\"kind-home-budget-logging\",namespace=\"home-budget\",app=\"home-budget-local\"} |= \"$probe\""
found=0
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --get \
    --cacert "$ca_file" \
    --data-urlencode "query=$query" \
    --data-urlencode 'limit=20' \
    https://localhost:13100/loki/api/v1/query_range | grep -Fq "$probe"; then
    found=1
    break
  fi
  sleep 2
done

if test "$found" -ne 1; then
  echo "Loki did not return $probe within 60 seconds." >&2
  echo "Inspect Alloy and Loki with: make dev-logging-logs" >&2
  "$compose" -p home-budget-local-logging \
    -f "$compose_file" ps >&2
  exit 1
fi

echo "PASS: $probe was emitted by home-budget and returned by Loki over TLS."
echo "Grafana: http://localhost:13000 (admin / local-only)"
echo "LogQL: $query"
