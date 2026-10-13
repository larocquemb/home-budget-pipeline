# Arsene native Ollama configuration

[Production CPU and development GPU runbook](../../docs/ollama-cpu-gpu-development.md)
documents KAN-135 rollout, validation, local profiles, and rollback.

Production retains `ollama.service` on `192.168.2.201:11434`, now CPU-only.
Development uses the new `ollama-development.service` on port 11436 and the
RTX 3090. Both use the existing model files; no production Kubernetes endpoint
configuration changes. The dedicated nftables table restricts all Ollama and
helper ports even when firewalld is inactive.

See [Ollama monitoring](../../docs/ollama-monitoring.md) for native journal
forwarding, continuous host/service/GPU metrics, the Grafana dashboard, and
deployment verification. Applying the role now loads `.env.monitoring` and
requires its existing monitoring client identity when journal collection is enabled.

This Git-managed Ansible playbook reconciles the existing Ollama host service.
It binds the API to `192.168.2.201:11434`, enables the service, restarts it only
when the managed configuration changes, and verifies `/api/tags` on the LAN address.
It does not install Ollama, download models, or move inference into Kubernetes.

From the repository root on your Mac, with the operations dependencies installed:

```sh
make ollama-gitops-syntax
make ollama-gitops-check
make ollama-gitops-apply
```

Check/apply prompt for Arsene's sudo password and use SSH as `paul`. Change host
settings in `inventory/production.yml`; review and commit them before applying.
Argo CD manages the application manifests, while this Ansible playbook manages
Arsene's systemd service and host firewall.

If firewalld is running, the role adds persistent **and** active source-specific
TCP rules for ports 11434 (production CPU), 11436 (development GPU), and 11435 (GPU telemetry) from the private LAN `192.168.2.0/24` and K3s pod network `10.42.0.0/16`.
It leaves other firewall rules in place and does not reload firewalld. Set
`ollama_firewall_zone` in the host inventory if Arsene's LAN interface belongs to
a zone other than firewalld's default. The dedicated ollama_access nftables table is always reconciled, including when firewalld is inactive.
The Ollama API has no authentication; these allowed networks are the trust boundary.

After applying, verify from the Mac and run a bounded comparison:

```sh
curl --fail --max-time 5 http://192.168.2.201:11434/api/tags
curl --fail --max-time 5 http://192.168.2.201:11435/gpu
make enrich-products COMPARE=1 LIMIT=1
```

The first model request loads Qwen automatically. No `ollama run` is necessary.
An existing manual `listen.conf` at the same path becomes managed by this role.

On Arsene, point the CLI at the managed listener before inspecting models:

```sh
export OLLAMA_HOST=http://192.168.2.201:11436
ollama ps
```

The role installs Python 3 and `ollama-gpu-telemetry.service`, which runs a fixed
read-only `nvidia-smi` query under Ollama's existing service account. Its `/gpu`
endpoint supplies GPU utilization, memory, power and temperature. It has no
model-management or command-execution API. Comparison jobs sample it during
Qwen calls and correlate observations with receipt/item/run identifiers. These
samples measure host activity, including any concurrent GPU workloads. Check
mode previews the files and firewall rules; endpoint verification runs on apply.

Receipt collaboration sets `OLLAMA_MAX_LOADED_MODELS=1` and
`OLLAMA_NUM_PARALLEL=1` through the managed override, and requests `keep_alive=5m`
to reuse a loaded model across stages. Ollama manages model replacement;
different Qwen profiles share the GPU sequentially. Profile
context/output budgets are configured separately from the host listener. Apply
host changes before the first collaboration run; model installation remains an
explicit host operation.

For a bounded collaborative review rather than a paid comparison, run:

```sh
make enrich-products COLLABORATE=1 LIMIT=1 RECEIPT='receipt.pdf' DRY_RUN=1
make enrich-products COLLABORATE=1 LIMIT=1 RECEIPT='receipt.pdf'
```

`DRY_RUN=1` validates the Job without calling models. A real collaboration uses
local Qwen and Brave, saving a recommendation for explicit acceptance in Ledger;
paid fallback requires `PAID_FALLBACK=1`. Inspect `ollama ps` and `nvidia-smi` on
Arsene during a call, and correlate sampled host GPU activity with the run UUID
in Grafana. A loaded model can remain in VRAM while utilization is zero between
requests. GPU samples describe the host, including other workloads; they do not
measure an isolated receipt's GPU time.

See the [collaboration and GPU manual](../../docs/product-model-comparison.md)
for model installation, budgets and telemetry, and the
[Ledger user guide](../../docs/user-guide.md) for review and acceptance.
