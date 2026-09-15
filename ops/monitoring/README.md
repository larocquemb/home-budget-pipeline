# External monitoring desired state

This directory turns the monitoring-host commands into a repeatable Ansible
reconciliation. Git owns the non-secret desired state for Loki, Tempo, Alloy,
Prometheus ingestion, nftables, Grafana datasources, and the Kubernetes
observability Secret shapes. Certificate private keys, Grafana credentials,
kubeconfigs, and CA private keys remain outside Git.

The committed `monitoring_loki_stage: enforced` state is the final comparison
configuration:

- Loki listens on `0.0.0.0:3100` so the K3s node can reach it.
- Loki requires a client certificate signed by the Brown Rook CA.
- Alloy and Grafana use their dedicated verified client identities.
- nftables permits only loopback and the approved K3s node on port 3100.
- The `fluent-bit-loki-tls` Secret is reconciled from external files, but the
  Fluent Bit overlay remains opt-in.
- Alloy accepts OTLP/gRPC with mTLS on `0.0.0.0:4317`; nftables admits only the
  approved K3s node and loopback.
- Tempo `3.0.3` runs in monolithic mode on loopback, stores traces under
  `/var/lib/tempo`, and retains blocks for 14 days.
- Alloy sends traces to loopback Tempo and writes converted application metrics
  to Prometheus's loopback-only remote-write receiver.
- Grafana owns a Tempo datasource with bidirectional trace/log navigation.

The role also keeps Loki at `info` log level and installs a journald drop-in
that caps persistent service logs at 256 MiB, reserves 1 GiB of filesystem
headroom, bounds individual journal files, and rate-limits bursts. These
guardrails prevent diagnostic logging from consuming the monitoring LXC's
memory, storage, and write endurance. Applying the role does not delete
existing journal history; any `journalctl --vacuum-*` cleanup is a separate,
explicit operator action.

Loki stores indexed logs under `/var/lib/loki` and its Compactor enforces a
14-day retention period. The first apply is an intentional clean cutover: after
Loki restarts and passes authenticated readiness against the durable store, the
role deletes the retired `/tmp/loki` store and its old log history. No migration
or manual cleanup is required.

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
host. It also reconciles the three telemetry TLS Secrets and the data-only OTLP
backend endpoint Secret. The playbook selects its explicit `brownrook-k3s1`
context rather than relying on the kubeconfig's current context. Private input
files and both kubeconfigs must be mode `0400` or `0600`.
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
leafs/otel-backend-server/otel-backend-server.{fullchain.crt,key}
leafs/receipt-telemetry-client/receipt-telemetry-client.{fullchain.crt,key}
leafs/otel-collector-server/otel-collector-server.{fullchain.crt,key}
leafs/otel-collector-backend-client/otel-collector-backend-client.{fullchain.crt,key}
```

`otel-backend-server` must have `serverAuth` and the
`monitoring.idc.brownrook.net` SAN. `otel-collector-server` must have
`serverAuth` and the `otel-collector.home-budget.svc.cluster.local` SAN. The
receipt and Collector-backend identities must have `clientAuth`. The playbook
checks each chain, key pair, purpose, server hostname, and minimum lifetime
before changing either host or Kubernetes state.

The CA signing keys are not playbook inputs. They remain offline or in the
YubiKey-backed ceremony and are needed only to issue or renew certificates.
The complete preparation, `gnutls-certtool` signing, full-chain construction,
and verification procedure is recorded in
[`docs/opentelemetry.md`](../../docs/opentelemetry.md#issue-or-rotate-telemetry-certificates).
Initial issuance is automated by
[`scripts/issue_telemetry_certificates.sh`](../../scripts/issue_telemetry_certificates.sh);
it still requires the Intermediate CA YubiKey, PIN prompts, and physical
touches.

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
only changed services, then Loki, Tempo, Prometheus, and Grafana datasource
health are tested. Secret-bearing tasks use Ansible's `no_log` protection.

Running `make monitoring-gitops-check` and then
`make monitoring-gitops-apply` again should report no configuration drift.

After an apply, verify the effective limits and current footprint without
printing log contents:

```sh
ssh paul@192.168.2.202 \
  'systemd-analyze cat-config systemd/journald.conf | grep -E "^(SystemMaxUse|SystemKeepFree|SystemMaxFileSize|RuntimeMaxUse|MaxRetentionSec|RateLimitIntervalSec|RateLimitBurst)="'
ssh paul@192.168.2.202 'journalctl --disk-usage'
ssh paul@192.168.2.202 \
  'sudo du -sh /var/lib/loki; sudo test ! -e /tmp/loki'
ssh paul@192.168.2.202 \
  'systemctl is-active tempo alloy prometheus; sudo du -sh /var/lib/tempo'
ssh paul@192.168.2.202 \
  'ss -lnt | grep -E "127.0.0.1:3200|127.0.0.1:9090|:4317"'
```

Reducing an already oversized journal is intentionally not automated because
it deletes retained operational history. After review, an operator can run
`sudo journalctl --vacuum-size=256M` on the monitoring host.

## mTLS stages and rollback

The final comparison state is selected with:

```yaml
monitoring_loki_stage: enforced
```

The enforced stage binds Loki to `0.0.0.0`, requires a verified client certificate,
and disables Loki's unauthenticated experimental metric-aggregation callback.
The OTLP listener is always mTLS-only and firewall restricted; Tempo and the
write-capable Prometheus API remain on loopback. Before applying this stage,
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

The telemetry overlay has a separate rollback: change the parent Application
path from `deploy/rabbitmq-private-telemetry` back to
`deploy/rabbitmq-private-logging`. Keep Tempo and its Kubernetes Secrets in
place until any queued Collector telemetry is drained; their presence does not
enable application telemetry by itself.
