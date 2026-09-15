#!/usr/bin/env bash
set -euo pipefail

image="${POSTGRES_OIDC_TEST_IMAGE:-home-budget-postgres:18.6-oidc-test}"
container="postgres-oidc-test-$$"

cleanup() {
  docker rm --force "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build \
  --file docker/postgres-oidc/Dockerfile \
  --tag "${image}" \
  .

version="$(docker run --rm "${image}" postgres --version)"
case "${version}" in
  "postgres (PostgreSQL) 18."*) ;;
  *)
    printf 'Unexpected PostgreSQL version: %s\n' "${version}" >&2
    exit 1
    ;;
esac

docker run --rm --entrypoint sh "${image}" -ec '
  library="$(pg_config --pkglibdir)/pg_oidc_validator.so"
  test -s "${library}"
  dependencies="$(ldd "${library}")"
  printf "%s\n" "${dependencies}"
  ! printf "%s\n" "${dependencies}" | grep -F "not found"
  grep -aF "pg_oidc_validator.audience" "${library}" >/dev/null
  grep -aF "The validator denies every token when this setting is empty." "${library}" >/dev/null
  test -s /usr/share/doc/pg_oidc_validator/LICENSE.txt
  test -s /usr/share/doc/pg_oidc_validator/LICENSE.jwt-cpp
'

docker run --rm --detach \
  --name "${container}" \
  --env POSTGRES_PASSWORD=test-only \
  "${image}" \
  -c oauth_validator_libraries=pg_oidc_validator \
  -c pg_oidc_validator.authn_field=oid \
  -c pg_oidc_validator.audience=test-audience >/dev/null

for _ in $(seq 1 30); do
  if docker exec "${container}" pg_isready --username postgres --dbname postgres >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker exec "${container}" pg_isready --username postgres --dbname postgres >/dev/null
docker exec --env PGPASSWORD=test-only "${container}" \
  psql --username postgres --dbname postgres --tuples-only --no-align \
  --command "SELECT current_setting('oauth_validator_libraries'), current_setting('pg_oidc_validator.authn_field'), current_setting('pg_oidc_validator.audience');" \
  | grep -Fx 'pg_oidc_validator|oid|test-audience' >/dev/null

printf 'Validated %s (%s)\n' "${image}" "${version}"
