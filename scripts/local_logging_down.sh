#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cluster_name=home-budget-logging
compose_file="$root_dir/deploy/local-logging/compose.yaml"
compose="$root_dir/scripts/docker_compose.sh"

"$compose" \
  -p home-budget-local-logging \
  -f "$compose_file" \
  down --volumes --remove-orphans

if kind get clusters | grep -Fxq "$cluster_name"; then
  kind delete cluster --name "$cluster_name"
fi

echo "Removed the disposable local logging containers and Kind cluster."
echo "Generated credentials and development certificates remain in .local-logging/."
