#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 MARKER [SINCE]" >&2
  echo "Example: $0 KAN86_LOKI_OUTAGE_PROBE 1h" >&2
  exit 2
}

test "$#" -ge 1 && test "$#" -le 2 || usage

marker=$1
since=${2:-1h}
namespace=${KUBE_NAMESPACE:-home-budget}
grafana_url=${GRAFANA_URL:-https://grafana.idc.brownrook.net}
datasource_uid=${GRAFANA_LOKI_DATASOURCE_UID:-cfy4qg8i178jkb}
grafana_user=${GRAFANA_ADMIN_USER:-admin}
pki_dir=${MONITORING_PKI_DIR:-"$HOME/brownrook-ca"}
root_ca=${BROWNROOK_ROOT_CA:-"$pki_dir/root/root_ca.crt"}

if [[ ! "$marker" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
  echo "MARKER may contain only letters, digits, dot, underscore, colon, and hyphen." >&2
  exit 2
fi

if [[ ! "$namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "KUBE_NAMESPACE must be a Kubernetes DNS label." >&2
  exit 2
fi

if [[ ! -r "$root_ca" ]]; then
  echo "Cannot read Brown Rook root CA: $root_ca" >&2
  exit 2
fi

for command_name in curl jq; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is not installed: $command_name" >&2
    exit 2
  fi
done

# Supplying only the username makes curl prompt without placing the Grafana
# password in shell history or the process arguments.
curl --fail --silent --show-error --user "$grafana_user" \
  --cacert "$root_ca" \
  --get \
  --data-urlencode "query={namespace=\"$namespace\"} |= \"$marker\"" \
  --data-urlencode "since=$since" \
  --data-urlencode 'direction=forward' \
  --data-urlencode 'limit=5000' \
  "$grafana_url/api/datasources/proxy/uid/$datasource_uid/loki/api/v1/query_range" \
  | jq -e '
      [
        .data.result[]
        | .stream.collection as $collection
        | .values[][1]
        | capture("sequence=(?<sequence>[0-9]+)")
        | {collection: $collection, sequence: (.sequence | tonumber)}
      ]
      | if length == 0 then error("no sequenced probe records found") else . end
      | group_by(.collection)
      | map({key: .[0].collection, value: map(.sequence)})
      | from_entries as $sets
      | def stats($values):
          if ($values | length) == 0 then
            {first: null, last: null, records: 0, unique_records: 0,
             duplicates: 0, missing: []}
          else
            ($values | unique) as $unique
            | {first: ($unique | min), last: ($unique | max),
               records: ($values | length), unique_records: ($unique | length),
               duplicates: (($values | length) - ($unique | length)),
               missing: ([range(($unique | min); (($unique | max) + 1))] - $unique)}
          end;
        ($sets["fluent-bit"] // []) as $fluent_bit
      | ($sets["kubernetes-api"] // []) as $kubernetes_api
      | {
          fluent_bit: stats($fluent_bit),
          kubernetes_api: stats($kubernetes_api),
          only_fluent_bit: (($fluent_bit | unique) - ($kubernetes_api | unique)),
          only_kubernetes_api: (($kubernetes_api | unique) - ($fluent_bit | unique))
        }
    '
