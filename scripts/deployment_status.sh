#!/usr/bin/env bash

set -uo pipefail

run_limit="${RUN_LIMIT:-3}"
argo_app="${ARGO_APP:-ledger}"
kube_namespace="${KUBE_NAMESPACE:-home-budget}"
ledger_deployment="${LEDGER_DEPLOYMENT:-ledger-web}"
exit_status=0
configured_image=''

section() {
    printf '\n== %s ==\n' "$1"
}

section "GitHub Actions (latest ${run_limit})"
if ! command -v gh >/dev/null 2>&1; then
    printf 'gh is not installed\n'
    exit_status=1
else
    if ! gh run list --branch main --limit "$run_limit" \
        --json conclusion,status,displayTitle,headBranch,headSha,databaseId,createdAt \
        --jq '(["STATUS", "TITLE", "BRANCH", "COMMIT", "RUN ID", "AGE"] | @tsv), (.[] | (now - (.createdAt | fromdateiso8601) | floor) as $age | [(if .conclusion == "" then .status else .conclusion end), .displayTitle, .headBranch, .headSha[0:7], .databaseId, (if $age < 3600 then "\($age / 60 | floor)m\($age % 60)s" elif $age < 86400 then "\($age / 3600 | floor)h\(($age % 3600) / 60 | floor)m" else "\($age / 86400 | floor)d\(($age % 86400) / 3600 | floor)h" end)] | @tsv)' \
        | column -t -s $'\t'; then
        exit_status=1
    fi
fi

section "Argo CD (${argo_app})"
if ! command -v argocd >/dev/null 2>&1; then
    printf 'argocd is not installed\n'
    exit_status=1
else
    argo_details=$(argocd app get "$argo_app" --grpc-web -o json | jq -r \
        '[.spec.source.targetRevision, .status.sync.revision[0:7], .status.sync.status, .status.health.status] | @tsv')
    argo_status=$?
    if [ "$argo_status" -ne 0 ]; then
        exit_status=1
    else
        IFS=$'\t' read -r argo_target argo_revision sync_status health_status <<< "$argo_details"
        if command -v kubectl >/dev/null 2>&1; then
            configured_image=$(kubectl -n "$kube_namespace" get deployment "$ledger_deployment" \
                -o jsonpath='{.spec.template.spec.containers[?(@.name=="ledger-web")].image}')
        fi
        application_commit="${configured_image##*:}"
        printf 'Target:        %s\n' "$argo_target"
        printf 'Revision:      %s -> Application: %.7s\n' "$argo_revision" "$application_commit"
        printf 'Sync Status:   %s\n' "$sync_status"
        printf 'Health Status: %s\n' "$health_status"
    fi
fi

section "Image verification (${kube_namespace})"
if ! command -v kubectl >/dev/null 2>&1; then
    printf 'kubectl is not installed\n'
    exit_status=1
else
    image_status=0
    if [ -z "$configured_image" ]; then
        configured_image=$(kubectl -n "$kube_namespace" get deployment "$ledger_deployment" \
            -o jsonpath='{.spec.template.spec.containers[?(@.name=="ledger-web")].image}')
        image_status=$?
    fi
    pod_details=$(kubectl -n "$kube_namespace" get pods -l app=ledger-web \
        --sort-by=.metadata.creationTimestamp \
        -o jsonpath='{.items[-1].metadata.name}{"\t"}{.items[-1].status.containerStatuses[?(@.name=="ledger-web")].imageID}')
    pod_status=$?

    if [ "$image_status" -ne 0 ] || [ "$pod_status" -ne 0 ] || [ -z "$configured_image" ]; then
        printf 'Unable to resolve the configured image or running pod\n'
        exit_status=1
    else
        IFS=$'\t' read -r pod_name pod_image_id <<< "$pod_details"
        application_commit="${configured_image##*:}"
        pod_digest="${pod_image_id##*@}"
        ghcr_digest='unavailable'
        match='UNKNOWN'

        if ! command -v docker >/dev/null 2>&1; then
            printf 'docker is not installed; GHCR digest lookup unavailable\n'
            exit_status=1
        else
            ghcr_digest=$(docker buildx imagetools inspect "$configured_image" \
                --format '{{json .Manifest}}' | jq -r '.digest')
            ghcr_status=$?
            if [ "$ghcr_status" -ne 0 ] || [ -z "$ghcr_digest" ] || [ "$ghcr_digest" = "null" ]; then
                ghcr_digest='unavailable'
                exit_status=1
            elif [ "$ghcr_digest" = "$pod_digest" ]; then
                match='YES'
            else
                match='NO'
                exit_status=1
            fi
        fi

        application_commit="${application_commit:0:7}"
        ghcr_digest_short="${ghcr_digest#sha256:}"
        pod_digest_short="${pod_digest#sha256:}"
        printf 'Application commit: %s\n' "$application_commit"
        printf 'GHCR digest:        %.7s\n' "$ghcr_digest_short"
        printf 'Pod digest:         %.7s\n' "$pod_digest_short"
        printf 'Match:              %s\n' "$match"
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
