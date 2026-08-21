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
    run_ids=$(gh run list --limit "$run_limit" --json databaseId --jq '.[].databaseId')
    list_status=$?
    if [ "$list_status" -ne 0 ]; then
        exit_status=1
    elif [ -z "$run_ids" ]; then
        printf 'No workflow runs found\n'
    else
        while IFS= read -r run_id; do
            details=$(gh run view "$run_id" \
                --json displayTitle,status,conclusion,updatedAt,url \
                --jq '"\(.displayTitle)\n  Status: \(.status) / \(.conclusion // "pending")\n  Completed: \(if .status == "completed" then .updatedAt else "not completed" end)\n  URL: \(.url)"')
            details_status=$?
            if [ "$details_status" -ne 0 ]; then
                exit_status=1
                continue
            fi
            printf '%s\n' "$details"

            failed_steps=$(gh run view "$run_id" --json jobs --jq \
                '[.jobs[] | .name as $job | .steps[] | select(.conclusion == "failure") | "\($job): \(.name)"] | join(", ")')
            errors_status=$?
            if [ "$errors_status" -ne 0 ]; then
                printf '  Errors: unable to inspect run jobs\n'
                exit_status=1
            elif [ -n "$failed_steps" ]; then
                printf '  Errors: %s\n' "$failed_steps"
            else
                printf '  Errors: none\n'
            fi
        done <<< "$run_ids"
    fi
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
