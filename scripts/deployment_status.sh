#!/usr/bin/env bash

set -uo pipefail

run_limit="${RUN_LIMIT:-3}"
argo_app="${ARGO_APP:-ledger}"
kube_namespace="${KUBE_NAMESPACE:-home-budget}"
exit_status=0

section() {
    printf '\n== %s ==\n' "$1"
}

section "GitHub Actions (latest ${run_limit})"
if ! command -v gh >/dev/null 2>&1; then
    printf 'gh is not installed\n'
    exit_status=1
else
    if ! gh run list --branch main --limit "$run_limit"; then
        exit_status=1
    fi
fi

section "Argo CD (${argo_app})"
if ! command -v argocd >/dev/null 2>&1; then
    printf 'argocd is not installed\n'
    exit_status=1
else
    if ! argocd app get "$argo_app" --grpc-web -o json | jq -r \
        '"Target:        \(.spec.source.targetRevision)\nRevision:      \(.status.sync.revision)\nSync Status:   \(.status.sync.status)\nHealth Status: \(.status.health.status)"'; then
        exit_status=1
    fi
fi

section "Kubernetes Jobs (${kube_namespace})"
if ! command -v kubectl >/dev/null 2>&1; then
    printf 'kubectl is not installed\n'
    exit_status=1
else
    kubectl -n "$kube_namespace" get jobs --sort-by=.metadata.creationTimestamp || exit_status=1
fi

section "Kubernetes Pods (${kube_namespace})"
if command -v kubectl >/dev/null 2>&1; then
    kubectl -n "$kube_namespace" get pods --sort-by=.metadata.name || exit_status=1
fi

exit "$exit_status"
