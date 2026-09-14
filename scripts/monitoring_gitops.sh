#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-}"

case "$mode" in
  syntax|check|apply)
    shift
    ;;
  *)
    echo "Usage: $0 {syntax|check|apply} [ansible-playbook options]" >&2
    exit 2
    ;;
esac

export ANSIBLE_CONFIG="$repository_root/ops/monitoring/ansible.cfg"
export ANSIBLE_LOCAL_TEMP="${ANSIBLE_LOCAL_TEMP:-/tmp/home-budget-monitoring-ansible}"
playbook="$repository_root/ops/monitoring/site.yml"
inventory="$repository_root/ops/monitoring/inventory/production.yml"

if [[ "$mode" == "syntax" ]]; then
  exec ansible-playbook --inventory "$inventory" "$playbook" --syntax-check "$@"
fi

environment_file="$repository_root/.env.monitoring"
if [[ ! -f "$environment_file" ]]; then
  echo "Copy ops/monitoring/env.example to .env.monitoring and set its paths." >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$environment_file"
set +a

required_variables=(
  MONITORING_PKI_DIR
  MONITORING_ALLOY_KUBECONFIG
  MONITORING_ADMIN_KUBECONFIG
)
for variable_name in "${required_variables[@]}"; do
  if [[ -z "${!variable_name:-}" ]]; then
    echo "Set $variable_name in .env.monitoring." >&2
    exit 2
  fi
done

if [[ -z "${MONITORING_GRAFANA_ADMIN_PASSWORD:-}" ]]; then
  read -r -s -p "Grafana admin password: " MONITORING_GRAFANA_ADMIN_PASSWORD
  echo
  export MONITORING_GRAFANA_ADMIN_PASSWORD
fi

connection_options=()
if [[ "${MONITORING_ASK_SSH_PASSWORD:-1}" == "1" ]]; then
  connection_options+=(--ask-pass)
fi
if [[ "${MONITORING_ASK_BECOME_PASSWORD:-1}" == "1" ]]; then
  connection_options+=(--ask-become-pass)
fi

if [[ "$mode" == "check" ]]; then
  exec ansible-playbook --inventory "$inventory" "$playbook" \
    "${connection_options[@]}" --check --diff "$@"
fi

exec ansible-playbook --inventory "$inventory" "$playbook" \
  "${connection_options[@]}" "$@"
