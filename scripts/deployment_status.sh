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
    run_rows=$(gh run list --limit "$run_limit" \
        --json databaseId,displayTitle,headBranch,headSha,status,updatedAt \
        --jq '.[] | (now - (.updatedAt | fromdateiso8601) | floor) as $age | [.databaseId, .displayTitle, .headBranch, .headSha, .headSha[0:7], .status, (if .status != "completed" then "-" elif $age < 3600 then "\($age / 60 | floor)m" elif $age < 86400 then "\($age / 3600 | floor)h" else "\($age / 86400 | floor)d" end)] | @tsv')
    list_status=$?
    if [ "$list_status" -ne 0 ]; then
        exit_status=1
    elif [ -z "$run_rows" ]; then
        printf 'No workflow runs found\n'
    else
        printf '%-48s  %-30s  %-5s  %-7s  %-12s  %-9s  %s\n' \
            'TITLE' 'BRANCH' 'PR' 'COMMIT' 'STATUS' 'COMPLETED' 'RUN ID'
        printf '%-48s  %-30s  %-5s  %-7s  %-12s  %-9s  %s\n' \
            '------------------------------------------------' \
            '------------------------------' \
            '-----' \
            '-------' \
            '------------' \
            '---------' \
            '------'
        while IFS=$'\t' read -r run_id title branch full_commit commit run_status completed; do
            pr_number=$(gh api "repos/{owner}/{repo}/commits/$full_commit/pulls" --jq '.[0].number // empty')
            pr_status=$?
            if [ "$pr_status" -ne 0 ]; then
                pr='#?'
                exit_status=1
            elif [ -n "$pr_number" ]; then
                pr="#$pr_number"
            else
                pr='-'
            fi
            if [[ "$title" =~ ^(.*)\ \(#[0-9]+\)$ ]]; then
                title="${BASH_REMATCH[1]}"
            fi
            printf '%-48.48s  %-30.30s  %-5.5s  %-7.7s  %-12.12s  %-9.9s  %s\n' \
                "$title" "$branch" "$pr" "$commit" "$run_status" "$completed" "$run_id"
        done <<< "$run_rows"
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
