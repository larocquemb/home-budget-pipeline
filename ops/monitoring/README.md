# External monitoring desired state

This directory turns the KAN-86 monitoring-host commands into a repeatable
Ansible reconciliation. Git owns the non-secret desired state for Loki, Alloy,
nftables, the Grafana Loki datasource, and the Kubernetes Fluent Bit TLS Secret
shape. Certificate private keys, Grafana credentials, kubeconfigs, and CA
private keys remain outside Git.

The committed `monitoring_loki_stage: enforced` state is the final comparison
configuration:

- Loki listens on `0.0.0.0:3100` so the K3s node can reach it.
- Loki requires a client certificate signed by the Brown Rook CA.
- Alloy and Grafana use their dedicated verified client identities.
- nftables permits only loopback and the approved K3s node on port 3100.
- The `fluent-bit-loki-tls` Secret is reconciled from external files, but the
  Fluent Bit overlay remains opt-in.

## External inputs

Copy the data-free environment example and set absolute paths:

```sh
cp ops/monitoring/env.example .env.monitoring
chmod 0600 .env.monitoring
```

`MONITORING_ALLOY_KUBECONFIG` is the namespace-scoped
`external-log-reader` credential installed on the monitoring host.
`MONITORING_ADMIN_KUBECONFIG` is a controller-only credential allowed to
create or update `home-budget/fluent-bit-loki-tls`; it is never copied to the
host. The playbook selects its explicit `brownrook-k3s1` context rather than
relying on the kubeconfig's current context. Private input files and both
kubeconfigs must be mode `0400` or `0600`.
The wrapper asks for the Grafana password without storing it in a file or shell
history.

The playbook expects the existing Brown Rook CA layout:

```text
root/root_ca.crt
intermediate/intermediate_ca.crt
leafs/monitoring/monitoring.{fullchain.crt,key}
leafs/alloy-loki-client/alloy-loki-client.{fullchain.crt,key}
leafs/grafana-loki-client/grafana-loki-client.{fullchain.crt,key}
leafs/fluent-bit-loki-client/fluent-bit-loki-client.{fullchain.crt,key}
```

The CA signing keys are not playbook inputs. They remain offline or in the
YubiKey-backed ceremony and are needed only to issue or renew certificates.

## Check and apply

Install the operations dependency once, validate the playbook locally, inspect
the remote diff, and then reconcile:

```sh
python -m pip install -e '.[ops]'
make monitoring-gitops-syntax
make monitoring-gitops-check
make monitoring-gitops-apply
```

The playbook verifies each certificate chain, expiry window, certificate/key
pair, and private-file mode before making changes. Config files are backed up
and validated with their native binaries before replacement. Handlers restart
only changed services, then authenticated Loki readiness and Grafana datasource
health are tested. Secret-bearing tasks use Ansible's `no_log` protection.

Running `make monitoring-gitops-check` and then
`make monitoring-gitops-apply` again should report no configuration drift.

## mTLS stages and rollback

The final comparison state is selected with:

```yaml
monitoring_loki_stage: enforced
```

The enforced stage binds Loki to `0.0.0.0`, requires a verified client certificate,
and disables Loki's unauthenticated experimental metric-aggregation callback.
The firewall remains the independent allowlist. Before applying this stage,
change `spec.source.path` for the Argo CD `ledger` Application in
`brownrook-infra/kubernetes/gitops/apps/ledger-app.yaml` to
`deploy/rabbitmq-private-logging`. The `brownrook-root` parent self-heals the
child Application from that repository, so a direct `argocd app set` override
will be reverted. After the playbook succeeds, let the parent reconcile, sync
the application, and verify the Fluent Bit DaemonSet as described in
`docs/log-forwarding.md`.

Rollback is a reviewed change to `optional`, followed by an apply; then change
the parent-owned Application manifest back to `deploy/rabbitmq-private`, merge,
and sync. Ansible's timestamped file backups provide a host-local emergency
recovery path, but Git is the normal source of truth.
