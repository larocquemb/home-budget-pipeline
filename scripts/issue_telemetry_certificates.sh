#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/issue_telemetry_certificates.sh [--pki-dir PATH] [--provider PATH]

Issue the four monitoring telemetry leaf identities with the Intermediate CA
YubiKey. The command refuses to replace an existing identity directory.

Defaults:
  --pki-dir   $MONITORING_PKI_DIR or $HOME/brownrook-ca
  --provider  $BROWNROOK_PKCS11_PROVIDER or /opt/homebrew/lib/libykcs11.dylib
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

if [[ -n "${MONITORING_PKI_DIR:-}" ]]; then
  telemetry_pki_dir=$MONITORING_PKI_DIR
else
  telemetry_pki_dir="${HOME:?HOME must be set}/brownrook-ca"
fi
telemetry_pkcs11_provider=${BROWNROOK_PKCS11_PROVIDER:-/opt/homebrew/lib/libykcs11.dylib}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pki-dir)
      [[ $# -ge 2 ]] || fail "--pki-dir requires a path"
      telemetry_pki_dir=$2
      shift 2
      ;;
    --provider)
      [[ $# -ge 2 ]] || fail "--provider requires a path"
      telemetry_pkcs11_provider=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      fail "unknown argument: $1"
      ;;
  esac
done

for telemetry_command in openssl p11tool ykman sed awk grep head mktemp; do
  command -v "$telemetry_command" >/dev/null 2>&1 \
    || fail "required command is not installed: $telemetry_command"
done

if command -v gnutls-certtool >/dev/null 2>&1; then
  telemetry_certtool=$(command -v gnutls-certtool)
elif [[ -x /opt/homebrew/opt/gnutls/bin/gnutls-certtool ]]; then
  telemetry_certtool=/opt/homebrew/opt/gnutls/bin/gnutls-certtool
else
  fail "gnutls-certtool is not installed"
fi

[[ -f "$telemetry_pkcs11_provider" ]] \
  || fail "PKCS#11 provider is missing: $telemetry_pkcs11_provider"

telemetry_leafs_dir="$telemetry_pki_dir/leafs"
telemetry_root_cert="$telemetry_pki_dir/root/root_ca.crt"
telemetry_intermediate_cert="$telemetry_pki_dir/intermediate/intermediate_ca.crt"
telemetry_client_config="$telemetry_leafs_dir/fluent-bit-loki-client/fluent-bit-loki-client.cnf"
telemetry_client_template="$telemetry_leafs_dir/fluent-bit-loki-client/fluent-bit-loki-client.tmpl"
telemetry_server_config="$telemetry_leafs_dir/monitoring/monitoring_leaf.cnf"
telemetry_server_template="$telemetry_leafs_dir/monitoring/monitoring_leaf.tmpl"

for telemetry_input in \
  "$telemetry_root_cert" \
  "$telemetry_intermediate_cert" \
  "$telemetry_client_config" \
  "$telemetry_client_template" \
  "$telemetry_server_config" \
  "$telemetry_server_template"
do
  [[ -f "$telemetry_input" ]] || fail "required PKI input is missing: $telemetry_input"
done

telemetry_identities=(
  otel-backend-server
  receipt-telemetry-client
  otel-collector-server
  otel-collector-backend-client
)

for telemetry_identity in "${telemetry_identities[@]}"; do
  [[ ! -e "$telemetry_leafs_dir/$telemetry_identity" ]] || fail \
    "refusing to replace existing identity: $telemetry_leafs_dir/$telemetry_identity"
done

umask 077
telemetry_staging_dir=$(mktemp -d "$telemetry_leafs_dir/.telemetry-issuance.XXXXXX")

cleanup() {
  case "${telemetry_staging_dir:-}" in
    "$telemetry_leafs_dir"/.telemetry-issuance.*)
      if [[ -d "$telemetry_staging_dir" ]]; then
        rm -rf -- "$telemetry_staging_dir"
      fi
      ;;
  esac
}
trap cleanup EXIT

generate_request() {
  local telemetry_name=$1
  local telemetry_curve=$2
  local telemetry_identity_dir="$telemetry_staging_dir/$telemetry_name"

  openssl genpkey \
    -algorithm EC \
    -pkeyopt "ec_paramgen_curve:$telemetry_curve" \
    -out "$telemetry_identity_dir/$telemetry_name.key"
  chmod 0600 "$telemetry_identity_dir/$telemetry_name.key"

  openssl req \
    -new \
    -sha384 \
    -config "$telemetry_identity_dir/$telemetry_name.cnf" \
    -key "$telemetry_identity_dir/$telemetry_name.key" \
    -out "$telemetry_identity_dir/$telemetry_name.csr"
  openssl req \
    -in "$telemetry_identity_dir/$telemetry_name.csr" \
    -noout \
    -verify
}

prepare_client() {
  local telemetry_name=$1
  local telemetry_identity_dir="$telemetry_staging_dir/$telemetry_name"

  mkdir -m 0755 "$telemetry_identity_dir"
  sed "s/^CN[[:space:]]*=.*/CN = $telemetry_name/" \
    "$telemetry_client_config" \
    >"$telemetry_identity_dir/$telemetry_name.cnf"
  cp "$telemetry_client_template" "$telemetry_identity_dir/$telemetry_name.tmpl"
  generate_request "$telemetry_name" prime256v1
}

prepare_server() {
  local telemetry_name=$1
  local telemetry_dns_name=$2
  local telemetry_identity_dir="$telemetry_staging_dir/$telemetry_name"

  mkdir -m 0755 "$telemetry_identity_dir"
  sed \
    -e "s/^CN[[:space:]]*=.*/CN = $telemetry_dns_name/" \
    -e "s/^DNS\\.1[[:space:]]*=.*/DNS.1 = $telemetry_dns_name/" \
    "$telemetry_server_config" \
    >"$telemetry_identity_dir/$telemetry_name.cnf"
  sed "s/^dns_name[[:space:]]*=.*/dns_name = $telemetry_dns_name/" \
    "$telemetry_server_template" \
    >"$telemetry_identity_dir/$telemetry_name.tmpl"
  generate_request "$telemetry_name" secp384r1
}

printf 'Preparing telemetry keys and certificate requests in protected staging...\n'
prepare_client receipt-telemetry-client
prepare_client otel-collector-backend-client
prepare_server otel-backend-server monitoring.idc.brownrook.net
prepare_server \
  otel-collector-server \
  otel-collector.home-budget.svc.cluster.local

telemetry_yubikey_cert="$telemetry_staging_dir/intermediate-from-yubikey.crt"
ykman piv certificates export 9c "$telemetry_yubikey_cert" >/dev/null

certificate_fingerprint() {
  openssl x509 -in "$1" -outform DER |
    openssl dgst -sha256 -r |
    awk '{print $1}'
}

telemetry_expected_fingerprint=$(certificate_fingerprint "$telemetry_intermediate_cert")
telemetry_token_fingerprint=$(certificate_fingerprint "$telemetry_yubikey_cert")
[[ "$telemetry_expected_fingerprint" == "$telemetry_token_fingerprint" ]] || fail \
  "YubiKey slot 9C certificate does not match $telemetry_intermediate_cert"
printf 'Verified that YubiKey slot 9C matches the Intermediate CA certificate.\n'

telemetry_private_key_urls=$(
  p11tool \
    --provider "$telemetry_pkcs11_provider" \
    --login \
    --list-all \
    --only-urls
)
telemetry_intermediate_key_uri=$(
  printf '%s\n' "$telemetry_private_key_urls" |
    awk '/id=%02/ && /type=private/ {print; exit}'
)
unset telemetry_private_key_urls

[[ -n "$telemetry_intermediate_key_uri" ]] \
  || fail "Intermediate CA private-key URI was not found in YubiKey slot 9C"
case "$telemetry_intermediate_key_uri" in
  *\\*) fail "the resolved PKCS#11 URI contains an unexpected backslash" ;;
esac

for telemetry_identity in "${telemetry_identities[@]}"; do
  telemetry_identity_dir="$telemetry_staging_dir/$telemetry_identity"
  printf '\nSigning %s; enter the Intermediate CA PIN and touch the YubiKey.\n' \
    "$telemetry_identity"
  "$telemetry_certtool" \
    --ask-pass \
    --hash=SHA384 \
    --generate-certificate \
    --template="$telemetry_identity_dir/$telemetry_identity.tmpl" \
    --load-request="$telemetry_identity_dir/$telemetry_identity.csr" \
    --load-ca-certificate="$telemetry_intermediate_cert" \
    --load-ca-privkey="$telemetry_intermediate_key_uri" \
    --provider="$telemetry_pkcs11_provider" \
    --outfile="$telemetry_identity_dir/$telemetry_identity.crt"

  cat \
    "$telemetry_identity_dir/$telemetry_identity.crt" \
    "$telemetry_intermediate_cert" \
    >"$telemetry_identity_dir/$telemetry_identity.fullchain.crt"
  chmod 0644 \
    "$telemetry_identity_dir/$telemetry_identity.cnf" \
    "$telemetry_identity_dir/$telemetry_identity.tmpl" \
    "$telemetry_identity_dir/$telemetry_identity.csr" \
    "$telemetry_identity_dir/$telemetry_identity.crt" \
    "$telemetry_identity_dir/$telemetry_identity.fullchain.crt"
done
unset telemetry_intermediate_key_uri

verify_identity() {
  local telemetry_name=$1
  local telemetry_purpose=$2
  local telemetry_hostname=${3:-}
  local telemetry_identity_dir="$telemetry_staging_dir/$telemetry_name"
  local telemetry_certificate_key
  local telemetry_private_key
  local telemetry_chain_count

  openssl verify \
    -CAfile "$telemetry_root_cert" \
    -untrusted "$telemetry_intermediate_cert" \
    -purpose "$telemetry_purpose" \
    "$telemetry_identity_dir/$telemetry_name.crt"
  openssl x509 \
    -checkend 604800 \
    -noout \
    -in "$telemetry_identity_dir/$telemetry_name.crt"

  if [[ -n "$telemetry_hostname" ]]; then
    openssl x509 \
      -checkhost "$telemetry_hostname" \
      -noout \
      -in "$telemetry_identity_dir/$telemetry_name.crt"
  fi

  telemetry_certificate_key=$(
    openssl x509 \
      -in "$telemetry_identity_dir/$telemetry_name.crt" \
      -pubkey \
      -noout |
      openssl pkey -pubin -outform DER |
      openssl dgst -sha256 -r |
      awk '{print $1}'
  )
  telemetry_private_key=$(
    openssl pkey \
      -in "$telemetry_identity_dir/$telemetry_name.key" \
      -pubout \
      -outform DER |
      openssl dgst -sha256 -r |
      awk '{print $1}'
  )
  [[ "$telemetry_certificate_key" == "$telemetry_private_key" ]] \
    || fail "certificate and private key do not match for $telemetry_name"

  telemetry_chain_count=$(grep -c -- \
    '-----BEGIN CERTIFICATE-----' \
    "$telemetry_identity_dir/$telemetry_name.fullchain.crt")
  [[ "$telemetry_chain_count" -eq 2 ]] \
    || fail "$telemetry_name full chain must contain leaf and Intermediate CA certificates"
}

printf '\nValidating signed telemetry identities...\n'
verify_identity receipt-telemetry-client sslclient
verify_identity otel-collector-backend-client sslclient
verify_identity \
  otel-backend-server \
  sslserver \
  monitoring.idc.brownrook.net
verify_identity \
  otel-collector-server \
  sslserver \
  otel-collector.home-budget.svc.cluster.local

for telemetry_identity in "${telemetry_identities[@]}"; do
  [[ ! -e "$telemetry_leafs_dir/$telemetry_identity" ]] || fail \
    "target appeared during issuance; refusing to replace it: $telemetry_identity"
  mv \
    "$telemetry_staging_dir/$telemetry_identity" \
    "$telemetry_leafs_dir/$telemetry_identity"
  printf 'Installed and validated %s.\n' "$telemetry_leafs_dir/$telemetry_identity"
done

printf '\nAll telemetry identities are ready. Next run:\n'
printf '  make monitoring-gitops-check\n'
