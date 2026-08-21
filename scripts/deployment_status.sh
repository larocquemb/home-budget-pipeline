#!/usr/bin/env bash

set -uo pipefail

run_limit="${RUN_LIMIT:-5}"
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
elif ! gh run list --limit "$run_limit"; then
    exit_status=1
fi

section "Argo CD (${argo_app})"
if ! command -v argocd >/dev/null 2>&1; then
    printf 'argocd is not installed\n'
    exit_status=1
else
    argo_output=$(argocd app get "$argo_app" --grpc-web 2>&1)
    argo_status=$?
    if [ "$argo_status" -eq 0 ]; then
        printf '%s\n' "$argo_output" | grep -E 'Target:|Sync Status:|Health Status:' || true
    else
        printf '%s\n' "$argo_output"
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
    kubectl -n "$kube_namespace" get pods --sort-by=.metadata.creationTimestamp || exit_status=1
fi

exit "$exit_status"

