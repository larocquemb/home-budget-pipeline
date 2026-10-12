#!/bin/sh
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
set -a
. "$repo_root/.local-services/telemetry/agent.env"
set +a
case "${1:-}" in
  alloy)
    exec "$LOCAL_ALLOY_BINARY" run --storage.path="$repo_root/.local-services/telemetry/data" \
      --server.http.listen-addr=127.0.0.1:12346 "$repo_root/deploy/local-telemetry/config.alloy"
    ;;
  *) echo "Usage: run_local_telemetry.sh alloy" >&2; exit 2 ;;
esac
