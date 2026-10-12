#!/usr/bin/env bash
set -euo pipefail

# Prepare only: never change PostgreSQL or invoke the CA signing key.
postgres_host=${1:?Usage: bash scripts/prepare_receipt_postgres_tls.sh HOSTNAME_OR_IP [OUTPUT_DIR]}
postgres_tls_dir=${2:-.local-services/postgres/tls}
if [[ "$postgres_host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  IFS=. read -r -a postgres_octets <<< "$postgres_host"
  for postgres_octet in "${postgres_octets[@]}"; do
    if (( 10#$postgres_octet > 255 )); then
      echo 'Invalid IPv4 address' >&2
      exit 1
    fi
  done
  postgres_san="IP:$postgres_host"
  postgres_template_name="ip_address"
else
  if [[ ${#postgres_host} -gt 253 || ! "$postgres_host" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$ ]]; then
    echo 'Host must be a DNS hostname (such as m4pro.local) or an IPv4 address' >&2
    exit 1
  fi
  IFS=. read -r -a postgres_labels <<< "$postgres_host"
  for postgres_label in "${postgres_labels[@]}"; do
    if [[ ${#postgres_label} -gt 63 ]]; then
      echo 'DNS hostname labels must be at most 63 characters' >&2
      exit 1
    fi
  done
  postgres_san="DNS:$postgres_host"
  postgres_template_name="dns_name"
fi
for postgres_artifact in server.key server.csr server.cnf server.tmpl; do
  if [[ -e "$postgres_tls_dir/$postgres_artifact" ]]; then
    echo "Refusing to replace $postgres_tls_dir/$postgres_artifact" >&2
    exit 1
  fi
done
umask 077
mkdir -p "$postgres_tls_dir"
chmod 0700 "$postgres_tls_dir"
cat > "$postgres_tls_dir/server.cnf" <<EOF
[req]
prompt = no
distinguished_name = subject
req_extensions = extensions
[subject]
CN = $postgres_host
[extensions]
subjectAltName = $postgres_san
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
EOF
cat > "$postgres_tls_dir/server.tmpl" <<EOF
cn = "$postgres_host"
organization = "Brown Rook"
expiration_days = 365
$postgres_template_name = "$postgres_host"
tls_www_server
signing_key
encryption_key
EOF
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out "$postgres_tls_dir/server.key" 2>/dev/null
openssl req -new -sha384 -config "$postgres_tls_dir/server.cnf" \
  -key "$postgres_tls_dir/server.key" -out "$postgres_tls_dir/server.csr"
openssl req -in "$postgres_tls_dir/server.csr" -verify -noout
printf 'Prepared %s/server.csr for %s. Keep server.key private.\n' \
  "$postgres_tls_dir" "$postgres_host"
