#!/usr/bin/env bash
# Isolated compatibility probe; never connects to existing databases.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
context="${1:-${script_dir}/../docker/postgres-oidc}"
image="localhost/brownrook-postgres-rhel-oidc:18.6-test"
container="brownrook-pg-rhel-probe-$$"

command -v podman >/dev/null
test -f "${context}/Dockerfile.rhel"
test -f "${context}/patches/0001-require-exact-audience-and-support-entra-v2.patch"

cleanup() {
  result=$?
  if podman container exists "${container}"; then
    if [ "${result}" -ne 0 ]; then
      podman logs --tail=100 "${container}" >&2 || true
    fi
    podman rm --force "${container}" >/dev/null || true
  fi
  return "${result}"
}
trap cleanup EXIT

podman build --jobs=2 --file "${context}/Dockerfile.rhel" \
  --tag "${image}" "${context}"

# No published ports, host storage, secrets or production network access.
# Fixed credentials are test-only and cannot be reached outside this container.
podman run --detach --name "${container}" --network=none \
  --memory=1g --memory-swap=1g --cpus=2 --pids-limit=256 \
  --env POSTGRESQL_USER=probe_user \
  --env POSTGRESQL_PASSWORD=isolated-test-only \
  --env POSTGRESQL_DATABASE=probe_db \
  "${image}" run-postgresql \
  -c shared_preload_libraries=pg_oidc_validator \
  -c oauth_validator_libraries=pg_oidc_validator \
  -c pg_oidc_validator.authn_field=oid \
  -c pg_oidc_validator.audience=isolated-test-audience >/dev/null

expected='pg_oidc_validator|oid|isolated-test-audience'
ready=false
for ((attempt=0; attempt<90; attempt++)); do
  settings="$(podman exec "${container}" psql -X -U postgres -d postgres \
    -v ON_ERROR_STOP=1 -Atc \
    "SELECT current_setting('oauth_validator_libraries'), current_setting('pg_oidc_validator.authn_field'), current_setting('pg_oidc_validator.audience');" \
    2>/dev/null || true)"
  if [ "${settings}" = "${expected}" ]; then
    ready=true
    break
  fi
  if [ "$(podman inspect --format '{{.State.Running}}' "${container}")" != true ]; then
    break
  fi
  sleep 1
done
if [ "${ready}" != true ]; then
  echo 'Red Hat startup or validator loading failed.' >&2
  exit 1
fi

# Exercise a real password-authenticated application connection over TCP.
podman exec --env PGPASSWORD=isolated-test-only "${container}" \
  psql -X -h 127.0.0.1 -U probe_user -d probe_db -v ON_ERROR_STOP=1 \
  -Atc 'SELECT current_database(), current_user;' \
  | grep -Fx 'probe_db|probe_user'

podman exec "${container}" postgres --version
podman image inspect "${image}" --format 'Runtime user: {{.Config.User}}'
printf '%s\n' \
  'PASS: Red Hat entrypoint, database initialization, validator loading, settings and password connection.' \
  'NOT TESTED: Entra token acceptance/rejection, role mapping, persistence, or vendor support coverage.'
