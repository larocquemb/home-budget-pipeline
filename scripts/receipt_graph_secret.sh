#!/usr/bin/env bash
# Initial provisioning only; never rotate credentials of an existing database.
set -euo pipefail
umask 077
context="${1:-brownrook-k3s1}"
namespace="${2:-home-budget}"
kube() { kubectl --context "$context" -n "$namespace" "$@"; }
if kube get secret neo4j-secret -o name >/dev/null 2>&1; then
  echo 'neo4j-secret already exists; preserving its credentials.'
  exit 0
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
password="$(openssl rand -hex 32)"
printf '%s\n' "NEO4J_AUTH=neo4j/$password" \
  'NEO4J_URI=bolt://neo4j.home-budget.svc.cluster.local:7687' \
  'NEO4J_USER=neo4j' "NEO4J_PASSWORD=$password" 'NEO4J_DATABASE=neo4j' > "$work/connection.env"
unset password
kube create secret generic neo4j-secret --from-env-file="$work/connection.env"
