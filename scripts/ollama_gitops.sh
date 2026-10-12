#!/usr/bin/env bash
set -euo pipefail
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-}"
shift || true
export ANSIBLE_CONFIG="$repository_root/ops/ollama/ansible.cfg"
export ANSIBLE_LOCAL_TEMP="${ANSIBLE_LOCAL_TEMP:-/tmp/home-budget-ollama-ansible}"
options=()
case "$mode" in
  syntax) options+=(--syntax-check) ;;
  check) options+=(--check --diff --ask-become-pass) ;;
  apply) options+=(--ask-become-pass) ;;
  *) echo "Usage: $0 {syntax|check|apply} [ansible-playbook options]" >&2; exit 2 ;;
esac
# Reuse the existing private Loki client identity; never put keys in Git.
if [[ "$mode" != "syntax" && -f "$repository_root/.env.monitoring" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$repository_root/.env.monitoring"
  set +a
fi
exec ansible-playbook -i "$repository_root/ops/ollama/inventory/production.yml" \
  "$repository_root/ops/ollama/site.yml" "${options[@]}" "$@"
