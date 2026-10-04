# Arsene Ollama configuration

This Git-managed Ansible playbook reconciles the existing Ollama host service.
It binds the API to `192.168.2.201:11434`, enables the service, restarts it only
when the systemd override changes, and verifies `/api/tags` on the LAN address.
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
TCP rules for the private LAN `192.168.2.0/24` and K3s pod network `10.42.0.0/16`.
It leaves other firewall rules in place and does not reload firewalld. Set
`ollama_firewall_zone` in the host inventory if Arsene's LAN interface belongs to
a zone other than firewalld's default. If firewalld is inactive, the role leaves
the firewall alone; any separately managed nftables policy must allow this traffic.
The Ollama API has no authentication; these allowed networks are the trust boundary.

After applying, verify from the Mac and run a bounded comparison:

```sh
curl --fail --max-time 5 http://192.168.2.201:11434/api/tags
make enrich-products COMPARE=1 LIMIT=1
```

The first model request loads Qwen automatically. No `ollama run` is necessary.
An existing manual `listen.conf` at the same path becomes managed by this role.
