#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
state_dir=${1:-"$root_dir/.local-logging"}
tls_dir="$state_dir/tls"
extension_file="$root_dir/deploy/local-logging/loki-cert-ext.cnf"

mkdir -p "$tls_dir"
chmod 700 "$state_dir" "$tls_dir"

if test -s "$tls_dir/ca.crt" \
  && test -s "$tls_dir/ca.key" \
  && test -s "$tls_dir/loki.crt" \
  && test -s "$tls_dir/loki.key" \
  && openssl x509 -checkend 86400 -noout -in "$tls_dir/loki.crt"; then
  echo "Reusing the unexpired local logging certificate."
  exit 0
fi

rm -f \
  "$tls_dir/ca.crt" \
  "$tls_dir/ca.key" \
  "$tls_dir/ca.srl" \
  "$tls_dir/loki.crt" \
  "$tls_dir/loki.csr" \
  "$tls_dir/loki.key"

openssl req -x509 -newkey rsa:3072 -sha384 -nodes -days 30 \
  -subj "/CN=Home Budget Local Logging CA" \
  -keyout "$tls_dir/ca.key" \
  -out "$tls_dir/ca.crt"

openssl req -newkey rsa:3072 -sha384 -nodes \
  -subj "/CN=loki" \
  -keyout "$tls_dir/loki.key" \
  -out "$tls_dir/loki.csr"

openssl x509 -req -sha384 -days 30 \
  -in "$tls_dir/loki.csr" \
  -CA "$tls_dir/ca.crt" \
  -CAkey "$tls_dir/ca.key" \
  -CAcreateserial \
  -extfile "$extension_file" \
  -out "$tls_dir/loki.crt"

chmod 600 "$tls_dir/ca.key" "$tls_dir/loki.key"
chmod 644 "$tls_dir/ca.crt" "$tls_dir/loki.crt"
rm -f "$tls_dir/loki.csr" "$tls_dir/ca.srl"

openssl verify -CAfile "$tls_dir/ca.crt" "$tls_dir/loki.crt"
echo "Generated a disposable 30-day TLS certificate in $tls_dir."
