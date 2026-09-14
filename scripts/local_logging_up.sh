#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cluster_name=home-budget-logging
kube_context=kind-$cluster_name
namespace=home-budget
state_dir="$root_dir/.local-logging"
kubeconfig="$state_dir/home-budget.kubeconfig"
compose_file="$root_dir/deploy/local-logging/compose.yaml"
compose="$root_dir/scripts/docker_compose.sh"

for command_name in docker kind kubectl openssl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 2
  fi
done

docker info >/dev/null
mkdir -p "$state_dir"
chmod 700 "$state_dir"

if ! kind get clusters | grep -Fxq "$cluster_name"; then
  kind create cluster \
    --name "$cluster_name" \
    --config "$root_dir/deploy/local-logging/kind-config.yaml"
else
  echo "Reusing Kind cluster $cluster_name."
fi

docker build \
  -f "$root_dir/deploy/local-logging/Dockerfile" \
  -t home-budget-pipeline:local \
  "$root_dir"

kind load docker-image \
  --name "$cluster_name" \
  home-budget-pipeline:local

kubectl --context "$kube_context" apply \
  -k "$root_dir/deploy/local-logging"
kubectl --context "$kube_context" -n "$namespace" rollout restart \
  deployment/home-budget-local
kubectl --context "$kube_context" -n "$namespace" rollout status \
  deployment/home-budget-local --timeout=180s

kubectl --context "$kube_context" apply \
  -f "$root_dir/k8s/external-log-reader-token.example.yaml"

token_ready=0
for _ in $(seq 1 30); do
  if kubectl --context "$kube_context" -n "$namespace" \
    get secret external-log-reader-token \
    -o jsonpath='{.data.token}' | grep -q .; then
    token_ready=1
    break
  fi
  sleep 1
done

if test "$token_ready" -ne 1; then
  echo "The local service-account token was not populated." >&2
  exit 1
fi

KUBE_CONTEXT="$kube_context" \
KUBE_CLUSTER_NAME="$kube_context" \
FORCE=1 \
  "$root_dir/scripts/export_external_log_reader_kubeconfig.sh" "$kubeconfig"

# The exporter first validates the credential through the host endpoint. Alloy
# runs outside Kind but joins Kind's Docker network, so give its copy the
# control-plane container endpoint instead.
kubectl --kubeconfig "$kubeconfig" config set-cluster "$kube_context" \
  --server="https://${cluster_name}-control-plane:6443" >/dev/null
chmod 600 "$kubeconfig"

"$root_dir/scripts/generate_local_logging_tls.sh" "$state_dir"

"$compose" \
  -p home-budget-local-logging \
  -f "$compose_file" \
  config --quiet
"$compose" \
  -p home-budget-local-logging \
  -f "$compose_file" \
  up -d

loki_container=$("$compose" \
  -p home-budget-local-logging \
  -f "$compose_file" \
  ps -q loki)
loki_ip=$(docker inspect \
  --format '{{with index .NetworkSettings.Networks "kind"}}{{.IPAddress}}{{end}}' \
  "$loki_container")
if ! [[ "$loki_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Could not determine Loki's address on the Kind network." >&2
  exit 1
fi

kubectl --context "$kube_context" -n "$namespace" create secret generic \
  fluent-bit-loki-tls \
  --type=kubernetes.io/tls \
  --from-file="ca.crt=$state_dir/tls/ca.crt" \
  --from-file="tls.crt=$state_dir/tls/fluent-bit-client.crt" \
  --from-file="tls.key=$state_dir/tls/fluent-bit-client.key" \
  --dry-run=client -o yaml \
  | kubectl --context "$kube_context" apply -f -

kubectl --context "$kube_context" apply \
  -k "$root_dir/deploy/local-logging/fluent-bit"
kubectl --context "$kube_context" -n "$namespace" patch endpointslice loki-local \
  --type=merge \
  -p "{\"endpoints\":[{\"addresses\":[\"$loki_ip\"]}]}" \
  >/dev/null

# Remove the pre-EndpointSlice resource left by older local environments without
# asking kubectl to resolve the deprecated resource type (which emits a warning).
legacy_loki_endpoint="/api/v1/namespaces/$namespace/endpoints/loki-local"
if kubectl --context "$kube_context" get --raw "$legacy_loki_endpoint" \
  >/dev/null 2>&1; then
  kubectl --context "$kube_context" delete --raw "$legacy_loki_endpoint" \
    >/dev/null 2>&1
fi

kubectl --context "$kube_context" -n "$namespace" rollout restart \
  daemonset/fluent-bit
kubectl --context "$kube_context" -n "$namespace" rollout status \
  daemonset/fluent-bit --timeout=180s

echo
echo "Local Method A and Method B logging environment is starting."
echo "Run: make dev-logging-test"
echo "Grafana: http://localhost:13000 (admin / local-only)"
echo "Alloy:   http://localhost:12345"
