#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 OUTPUT.kubeconfig" >&2
  exit 2
}

test "$#" -eq 1 || usage

output_path=$1
kube_context=${KUBE_CONTEXT:-brownrook-k3s1}
cluster_name=${KUBE_CLUSTER_NAME:-$kube_context}
namespace=${KUBE_NAMESPACE:-home-budget}
secret_name=${EXTERNAL_LOG_TOKEN_SECRET:-external-log-reader-token}
credential_name=${KUBE_CREDENTIAL_NAME:-external-log-reader}
generated_context=${KUBE_GENERATED_CONTEXT:-external-log-reader@$cluster_name}

if test -e "$output_path" && test "${FORCE:-0}" != 1; then
  echo "Refusing to replace $output_path; set FORCE=1 to rotate it." >&2
  exit 2
fi

api_server=$(kubectl --context "$kube_context" config view --minify --raw \
  -o jsonpath='{.clusters[0].cluster.server}')
ca_data=$(kubectl --context "$kube_context" -n "$namespace" get secret "$secret_name" \
  -o jsonpath='{.data.ca\.crt}')
token_data=$(kubectl --context "$kube_context" -n "$namespace" get secret "$secret_name" \
  -o jsonpath='{.data.token}')

if test -z "$api_server" || test -z "$ca_data" || test -z "$token_data"; then
  echo "Secret $namespace/$secret_name is missing its token or CA data." >&2
  exit 1
fi

if token=$(printf '%s' "$token_data" | base64 --decode 2>/dev/null); then
  :
else
  token=$(printf '%s' "$token_data" | base64 -D)
fi

umask 077
temp_file=$(mktemp "${TMPDIR:-/tmp}/home-budget-kubeconfig.XXXXXX")
trap 'rm -f "$temp_file"' EXIT

{
  printf '%s\n' 'apiVersion: v1' 'kind: Config' 'clusters:'
  printf -- '- name: %s\n' "$cluster_name"
  printf '%s\n' '  cluster:'
  printf '    certificate-authority-data: %s\n' "$ca_data"
  printf '    server: %s\n' "$api_server"
  printf '%s\n' 'users:'
  printf -- '- name: %s\n' "$credential_name"
  printf '%s\n' '  user:'
  printf '    token: %s\n' "$token"
  printf '%s\n' 'contexts:'
  printf -- '- name: %s\n' "$generated_context"
  printf '%s\n' '  context:'
  printf '    cluster: %s\n' "$cluster_name"
  printf '    namespace: %s\n' "$namespace"
  printf '    user: %s\n' "$credential_name"
  printf 'current-context: %s\n' "$generated_context"
} > "$temp_file"

unset token token_data

kubectl --kubeconfig "$temp_file" get pods -n "$namespace" \
  --request-timeout=10s >/dev/null
install -m 600 "$temp_file" "$output_path"
echo "Wrote and validated $output_path" >&2
