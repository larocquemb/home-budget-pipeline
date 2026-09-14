# Kubernetes log forwarding

Two independent collectors can observe the same application stdout/stderr:

Method B is the current production path:

```text
home-budget Pods -> Kubernetes API (TLS) -> external Alloy -> Loki (TLS) -> Grafana
```

Grafana Alloy runs as a systemd service on `monitoring.idc.brownrook.net`, not in
K3s. It discovers and tails only Pods in the `home-budget` namespace through the
Kubernetes API and writes them to the existing Loki instance on the monitoring
host. This is the vendor-compatible path because it installs nothing in the
application cluster.

Method A is an opt-in overlay for BrownRook-managed clusters:

```text
home-budget Pods -> Fluent Bit DaemonSet -> Loki -> Grafana
```

Fluent Bit tails the node's CRI log files, enriches them with namespace-scoped
Kubernetes metadata, and writes directly through Loki's native HTTPS API. The
collectors use `collection="kubernetes-api"` and `collection="fluent-bit"` so
both copies can coexist during comparison.

## Develop and test both methods locally

Use the disposable local environment before changing production K3s. It runs
entirely on the Mac through the active Docker/Colima backend:

```text
home-budget in Kind -> Kind API (TLS) -> external Alloy container --+
       |                                                        |
       +-> node CRI logs -> Fluent Bit DaemonSet ----------------+-> local Loki (TLS) -> local Grafana
```

All Kubernetes commands in the local scripts explicitly use the
`kind-home-budget-logging` context. They do not read from or deploy to
`brownrook-k3s1`. Alloy joins Kind's Docker network but is not deployed inside
the cluster, preserving the production Method B trust boundary. Fluent Bit runs
inside Kind as it does for Method A.

The local image runs the real `home_budget_pipeline.web.receipt_app` FastAPI
application with Uvicorn access logging. It uses the database-independent
`/ledger/health` endpoint, so the logging test does not copy `.env.dev`, database
credentials, receipt data, or production certificates into Kind. Continue to
use `make dev-up` and `make dev-web` for the existing host-based application
development environment.

Select Colima as the Docker backend and confirm it is ready:

```sh
colima status
docker context use colima
docker info --format '{{.ServerVersion}} {{.Architecture}}'
```

If Colima is installed but stopped, start it first:

```sh
colima start --cpu 4 --memory 8 --disk 40
```

Create or update the local environment. This builds the lightweight local
application image, loads it into Kind, deploys both collectors, generates a
disposable 30-day CA plus server/client certificates, exports a local restricted
kubeconfig, and starts Alloy, Loki, and Grafana:

```sh
make dev-logging-up
```

Run the end-to-end test:

```sh
make dev-logging-test
```

The test sends one uniquely named request to the application, confirms that the
request is in the `home-budget-local` Pod's Uvicorn log, and queries Loki over
verified HTTPS for that marker through each `collection` label. A passing run
prints both exact Grafana LogQL queries.

Open the local interfaces:

- Grafana: `http://localhost:13000` (`admin` / `local-only`)
- Alloy: `http://localhost:12345`
- Loki HTTPS API: `https://localhost:13100`

The credentials and development certificates live in the ignored
`.local-logging/` directory. They are disposable and are never used by the
production deployment. The local Grafana and Loki data is ephemeral.

After changing application or collector source, rebuild and retest:

```sh
make dev-logging-up
make dev-logging-test
```

Inspect status or follow collector logs:

```sh
make dev-logging-status
make dev-logging-logs
kubectl --context kind-home-budget-logging -n home-budget \
  logs daemonset/fluent-bit
```

Remove the local containers and Kind cluster when finished:

```sh
make dev-logging-down
```

The down target intentionally retains `.local-logging/` so subsequent runs can
reuse the unexpired development certificate. Deleting that ignored directory
manually forces regeneration on the next `make dev-logging-up`.

## Security boundaries

The external collector authenticates as the `external-log-reader` service
account. Its namespace `Role` permits only `get`, `list`, and `watch` on Pods and
`get` on the `pods/log` subresource. It cannot read Secrets, ConfigMaps, Nodes,
or other namespaces. `automountServiceAccountToken` is disabled because no Pod
uses this identity.

Method B uses two independently verified TLS connections:

1. The generated kubeconfig embeds the K3s API CA and uses
   `https://k3s1.brownrook.net:6443`.
2. Alloy sends to Loki over HTTPS and validates the dedicated
   `monitoring.idc.brownrook.net` certificate against the Brown Rook root CA.

Method A validates the same Loki server certificate and authenticates with a
dedicated client certificate stored in the `fluent-bit-loki-tls` Secret. Loki
must require a private-CA client certificate before port 3100 is bound to a LAN
address. No collector configuration enables `insecure_skip_verify`.

The API credential is a manually provisioned service-account token Secret. This
long-lived credential supports an always-running external collector but must be
protected and rotated. The populated Secret, exported kubeconfig, private key,
and populated certificates stay outside Git. Delete the token Secret to revoke
the collector immediately.

Both collectors promote only the bounded `cluster`, `environment`, `collection`,
`namespace`, `pod`, `container`, `node`, `app`, `service`, and `job` fields to
Loki labels. They do not copy arbitrary Pod labels or annotations. Receipt
filenames, source hashes, request IDs, `run_uuid`, and `pass_id` remain message
content rather than labels. Both paths redact common PostgreSQL URL, API key,
token, password, and Bearer-token forms. This is defense in depth; applications
must still avoid logging credentials and personal data.

Fluent Bit receives a namespace `Role` that can only `get`, `list`, and `watch`
Pods in `home-budget`; it cannot read Secrets or resources in another namespace.
Node root access is still materially stronger than Method B: the DaemonSet reads
`/var/log`, and its filesystem buffer persists under
`/var/lib/home-budget/fluent-bit`. That privilege is why Method A is limited to
BrownRook-managed clusters.

## Git-controlled production reconciliation

The production commands are captured in `ops/monitoring` as an idempotent
Ansible playbook. Git holds the Loki and nftables templates, the exact Alloy
configuration, the Grafana datasource shape, the Kubernetes Secret shape, and
the rollout stage. Private keys, kubeconfigs, Grafana credentials, and CA
signing keys are external inputs and are never rendered into the repository or
Ansible output.

This is a Git-controlled push workflow rather than a continuously running pull
controller for the Debian monitoring host. `check` detects drift and `apply`
reconciles it; Argo CD continues to own Kubernetes workload manifests. Prepare
the ignored environment file, validate, inspect the remote diff, and apply:

```sh
cp ops/monitoring/env.example .env.monitoring
chmod 0600 .env.monitoring
python -m pip install -e '.[ops]'

make monitoring-gitops-syntax
make monitoring-gitops-check
make monitoring-gitops-apply
```

The `optional` stage is the safe bootstrap and rollback checkpoint: Loki stays
on loopback with `VerifyClientCertIfGiven`, while Alloy and Grafana use their
dedicated identities. The playbook also reconciles the Fluent Bit TLS Secret
with a controller-only Kubernetes credential. The committed `enforced` stage
binds Loki to the firewall-restricted LAN listener and selects
`RequireAndVerifyClientCert`; apply it only as part of the Fluent Bit overlay
rollout.

The full input contract, ordering, idempotency behavior, and rollback are in
the [monitoring automation README](https://github.com/larocquemb/home-budget-pipeline/tree/main/ops/monitoring).
The command-by-command sections below remain the certificate-ceremony and
emergency-recovery reference; the playbook is the normal repeatable path.

## Deploy the Kubernetes access policy

The base Kustomization includes
[`external-log-reader-rbac.yaml`](https://github.com/larocquemb/home-budget-pipeline/blob/main/k8s/external-log-reader-rbac.yaml),
so every current deployment overlay includes the same namespace-scoped access
policy. Render and deploy it through the existing Argo CD application:

```sh
kubectl kustomize deploy/rabbitmq-private \
  | rg -n 'external-log-reader|pods/log'

argocd app sync ledger --grpc-web
argocd app wait ledger --sync --health --timeout 600 --grpc-web
```

Confirm that the live policy is constrained:

```sh
SA='system:serviceaccount:home-budget:external-log-reader'

kubectl --context brownrook-k3s1 auth can-i --as="$SA" list pods -n home-budget
kubectl --context brownrook-k3s1 auth can-i --as="$SA" get pods/log -n home-budget
kubectl --context brownrook-k3s1 auth can-i --as="$SA" get secrets -n home-budget
kubectl --context brownrook-k3s1 auth can-i --as="$SA" list pods -n default
```

The expected answers are `yes`, `yes`, `no`, and `no`.

## Provision the external kubeconfig

Create the untracked token Secret from its data-free template after Argo CD has
created the service account:

```sh
kubectl --context brownrook-k3s1 apply \
  -f k8s/external-log-reader-token.example.yaml
```

Do not print or export the Secret as YAML. Generate a mode-0600 kubeconfig with
the repository helper. It verifies that the resulting credential can list the
namespace before writing the requested output:

```sh
mkdir -p .monitoring
chmod 700 .monitoring
scripts/export_external_log_reader_kubeconfig.sh \
  .monitoring/home-budget.kubeconfig
```

Verify access and denial boundaries without displaying the token:

```sh
kubectl --kubeconfig .monitoring/home-budget.kubeconfig get pods
kubectl --kubeconfig .monitoring/home-budget.kubeconfig auth can-i get pods/log
kubectl --kubeconfig .monitoring/home-budget.kubeconfig auth can-i get secrets
```

The last command must report `no`. Files ending in `.kubeconfig` are ignored by
Git.

## Manual reference: prepare TLS and client identities

Issue the dedicated server leaf certificate from the Brown Rook intermediate
CA. Its SANs should include `monitoring.idc.brownrook.net` and
`grafana.idc.brownrook.net`. Keep `monitoring.key` only in the protected
external PKI directory and on the monitoring host; the playbook transfers it
with `no_log` and installs it as `0640 root:monitoring-tls`.

Issue three separate private-CA client identities with the `clientAuth` extended
key usage: `alloy-loki-client`, `grafana-loki-client`, and
`fluent-bit-loki-client`. Keep each private key only on the system that uses it.
Using separate identities makes ownership clear even though this evaluation
does not configure certificate revocation checking.

Copy the public root CA, full chain, external kubeconfig, and Alloy configuration
from the Mac:

```sh
scp ~/brownrook-ca/root/root_ca.crt \
  ~/brownrook-ca/leafs/monitoring/monitoring.fullchain.crt \
  .monitoring/alloy-loki-client.crt \
  .monitoring/alloy-loki-client.key \
  .monitoring/grafana-loki-client.crt \
  .monitoring/grafana-loki-client.key \
  .monitoring/home-budget.kubeconfig \
  deploy/external-logging/config.alloy \
  paul@192.168.2.202:/tmp/
```

On the monitoring host, install the certificate for Loki and Grafana. The
private key path below assumes it was generated on that host as part of the CSR
workflow:

```sh
getent group monitoring-tls >/dev/null || sudo groupadd --system monitoring-tls
sudo usermod -aG monitoring-tls loki
sudo usermod -aG monitoring-tls grafana

sudo install -d -m 0750 -o root -g monitoring-tls /etc/monitoring/tls
sudo install -m 0644 -o root -g root \
  /tmp/monitoring.fullchain.crt \
  /etc/monitoring/tls/monitoring.fullchain.crt
sudo install -m 0640 -o root -g monitoring-tls \
  ~/pki/monitoring/monitoring.key \
  /etc/monitoring/tls/monitoring.key

sudo install -m 0644 -o root -g root \
  /tmp/root_ca.crt \
  /usr/local/share/ca-certificates/brown-rook-root-ca.crt
sudo install -m 0644 -o root -g root \
  /tmp/root_ca.crt \
  /etc/monitoring/tls/brown-rook-client-ca.crt
sudo update-ca-certificates
```

## Manual reference: install and configure external Alloy

Install the official package on the Debian monitoring host:

```sh
sudo apt-get update
sudo apt-get install -y gpg wget
sudo mkdir -p /etc/apt/keyrings
sudo wget -O /etc/apt/keyrings/grafana.asc \
  https://apt.grafana.com/gpg-full.key
sudo chmod 0644 /etc/apt/keyrings/grafana.asc
echo 'deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main' \
  | sudo tee /etc/apt/sources.list.d/grafana.list >/dev/null
sudo apt-get update
sudo apt-get install -y alloy
```

Install the configuration and credentials with restricted permissions:

```sh
sudo install -d -m 0750 -o root -g alloy /etc/alloy
sudo install -m 0640 -o root -g alloy \
  /tmp/home-budget.kubeconfig /etc/alloy/home-budget.kubeconfig
sudo install -m 0640 -o root -g alloy \
  /tmp/root_ca.crt /etc/alloy/brown-rook-root-ca.crt
sudo install -m 0644 -o root -g root \
  /tmp/alloy-loki-client.crt /etc/alloy/loki-client.crt
sudo install -m 0640 -o root -g alloy \
  /tmp/alloy-loki-client.key /etc/alloy/loki-client.key
sudo install -m 0640 -o root -g alloy \
  /tmp/config.alloy /etc/alloy/config.alloy
sudo chown -R alloy:alloy /var/lib/alloy
sudo chmod 0750 /var/lib/alloy

sudo -u alloy /usr/bin/alloy validate /etc/alloy/config.alloy
sudo systemctl enable --now alloy
sudo systemctl --no-pager --full status alloy
sudo journalctl -u alloy -n 100 --no-pager
```

Configure the Grafana Loki data source with `tlsAuth: true`,
`tlsAuthWithCACert: true`, the Brown Rook root CA, and its separate Grafana
client certificate/key. Grafana stores `tlsCACert`, `tlsClientCert`, and
`tlsClientKey` as encrypted secure data-source fields. Update its URL to
`https://monitoring.idc.brownrook.net:3100` and keep this host-local resolution
so Grafana uses loopback even when Loki later listens on the LAN. Use
`/tmp/root_ca.crt`, `/tmp/grafana-loki-client.crt`, and
`/tmp/grafana-loki-client.key` as the three inputs. The data-free
[`grafana-datasource.example.yaml`](https://github.com/larocquemb/home-budget-pipeline/blob/main/deploy/external-logging/grafana-datasource.example.yaml)
records the reproducible provisioning shape; populate only an untracked copy:

```text
127.0.0.1 monitoring.idc.brownrook.net
```

Test Alloy and Grafana with their client identities while Loki still accepts
clients without certificates. Only then merge the final `server` settings from
[`loki-tls-config.example.yaml`](https://github.com/larocquemb/home-budget-pipeline/blob/main/deploy/external-logging/loki-tls-config.example.yaml)
into `/etc/loki/config.yml`; retain the existing storage, schema, and retention
configuration:

```sh
sudo cp -a /etc/loki/config.yml \
  "/etc/loki/config.yml.backup-$(date +%Y%m%d-%H%M%S)"
sudoedit /etc/loki/config.yml
```

The comparison listener requires a valid client identity on every connection:

```yaml
server:
  http_listen_address: 0.0.0.0
  http_listen_port: 3100
  tls_min_version: VersionTLS12
  http_tls_config:
    cert_file: /etc/monitoring/tls/monitoring.fullchain.crt
    key_file: /etc/monitoring/tls/monitoring.key
    client_auth_type: RequireAndVerifyClientCert
    client_ca_file: /etc/monitoring/tls/brown-rook-client-ca.crt
```

Validate and restart Loki, then prove that an authenticated request succeeds:

```sh
sudo -u loki /usr/bin/loki \
  -config.file=/etc/loki/config.yml -verify-config
sudo systemctl restart loki
sudo systemctl --no-pager --full status loki

curl --cacert /tmp/root_ca.crt \
  --cert /tmp/alloy-loki-client.crt \
  --key /tmp/alloy-loki-client.key \
  --resolve monitoring.idc.brownrook.net:3100:127.0.0.1 \
  https://monitoring.idc.brownrook.net:3100/ready
```

Grafana must still report a healthy Loki data source. A request without a client
certificate must now fail the TLS handshake. Restrict TCP 3100 at the
monitoring-host firewall to `192.168.2.230/32` plus loopback as an independent
network control.

Remove the temporary credential and certificate copies after both services are
healthy:

```sh
rm -f /tmp/home-budget.kubeconfig /tmp/config.alloy
rm -f /tmp/root_ca.crt /tmp/monitoring.fullchain.crt
rm -f /tmp/alloy-loki-client.crt /tmp/alloy-loki-client.key
rm -f /tmp/grafana-loki-client.crt /tmp/grafana-loki-client.key
```

## Provision and deploy Fluent Bit

The normal Ansible reconciliation creates the in-cluster TLS Secret from the
public server CA and dedicated Fluent Bit client identity without logging its
contents. The example manifest documents the required keys but is deliberately
excluded from Kustomize. The following command is retained for manual recovery:

```sh
kubectl --context brownrook-k3s1 -n home-budget create secret generic \
  fluent-bit-loki-tls \
  --type=kubernetes.io/tls \
  --from-file=ca.crt="$HOME/brownrook-ca/root/root_ca.crt" \
  --from-file=tls.crt=.monitoring/fluent-bit-loki-client.crt \
  --from-file=tls.key=.monitoring/fluent-bit-loki-client.key \
  --dry-run=client -o yaml \
  | kubectl --context brownrook-k3s1 apply -f -
```

Check the opt-in overlay before changing Argo CD. It retains the current
RabbitMQ and private-ingress resources and adds one Fluent Bit Pod per Linux
node:

```sh
kubectl kustomize deploy/rabbitmq-private-logging \
  | rg -n 'kind: DaemonSet|name: fluent-bit|monitoring.idc.brownrook.net'
```

The `ledger` Application is managed by the `brownrook-root` app-of-apps. Its
source path is declared in
`brownrook-infra/kubernetes/gitops/apps/ledger-app.yaml`. Change that manifest's
`spec.source.path` to `deploy/rabbitmq-private-logging`, merge it to the
infrastructure repository's `main` branch, and let the parent reconcile it.
Do not use `argocd app set` for this change: parent self-healing restores the
Git-declared value. After the parent has reconciled, confirm the path and sync
the workload:

```sh
argocd app wait brownrook-root --sync --health --timeout 600 --grpc-web
argocd app get ledger --grpc-web -o json \
  | jq -e '.spec.source.path == "deploy/rabbitmq-private-logging"'
argocd app sync ledger --grpc-web
argocd app wait ledger --sync --health --timeout 600 --grpc-web
```

Verify scheduling, namespace-only API permission, TLS delivery, storage, and
delivery metrics:

```sh
kubectl --context brownrook-k3s1 -n home-budget rollout status \
  daemonset/fluent-bit --timeout=180s
kubectl --context brownrook-k3s1 -n home-budget get daemonset/fluent-bit -o wide
kubectl --context brownrook-k3s1 auth can-i \
  --as=system:serviceaccount:home-budget:fluent-bit list pods -n home-budget
kubectl --context brownrook-k3s1 auth can-i \
  --as=system:serviceaccount:home-budget:fluent-bit get secrets -n home-budget
kubectl --context brownrook-k3s1 -n home-budget logs daemonset/fluent-bit --tail=100
kubectl --context brownrook-k3s1 -n home-budget port-forward \
  service/fluent-bit 2020:2020
```

The RBAC answers must be `yes` and `no`. While the port-forward is running,
inspect `/api/v2/health`, `/api/v2/metrics/prometheus`, and `/api/v1/storage` on
`http://127.0.0.1:2020`. The metrics include processed, retried, failed, and
dropped records; the storage endpoint shows filesystem-buffer use.

## Prove dual end-to-end collection

Create a short-lived, non-sensitive test log from the Mac:

```sh
PROBE="dual-collector-probe-$(date +%s)"
kubectl --context brownrook-k3s1 -n home-budget run "$PROBE" \
  --image=busybox:1.37 --restart=Never -- \
  sh -c "echo DUAL_COLLECTOR_PROBE_${PROBE}; sleep 15"
kubectl --context brownrook-k3s1 -n home-budget wait \
  --for=condition=Ready "pod/$PROBE" --timeout=60s
```

On the monitoring host, query Loki through verified TLS:

```sh
curl --fail --silent --show-error --get \
  --cacert /etc/alloy/brown-rook-root-ca.crt \
  --cert /etc/alloy/loki-client.crt \
  --key /etc/alloy/loki-client.key \
  --resolve monitoring.idc.brownrook.net:3100:127.0.0.1 \
  --data-urlencode 'query={cluster="brownrook-k3s1",namespace="home-budget",collection="kubernetes-api"} |= "DUAL_COLLECTOR_PROBE"' \
  --data-urlencode 'limit=20' \
  https://monitoring.idc.brownrook.net:3100/loki/api/v1/query_range

curl --fail --silent --show-error --get \
  --cacert /etc/alloy/brown-rook-root-ca.crt \
  --cert /etc/alloy/loki-client.crt \
  --key /etc/alloy/loki-client.key \
  --resolve monitoring.idc.brownrook.net:3100:127.0.0.1 \
  --data-urlencode 'query={cluster="brownrook-k3s1",namespace="home-budget",collection="fluent-bit"} |= "DUAL_COLLECTOR_PROBE"' \
  --data-urlencode 'limit=20' \
  https://monitoring.idc.brownrook.net:3100/loki/api/v1/query_range
```

Both LogQL queries in Grafana Explore must return the same probe line with its
`namespace`, `pod`, `container`, `node`, `app`, `service`, `job`, and
`collection` labels. Delete the probe Pod after verification.

## Rotation and rollback

To rotate the Kubernetes API credential, delete and recreate the token Secret,
export a new kubeconfig with `FORCE=1`, install it on the monitoring host, and
restart Alloy. To revoke Method B immediately:

```sh
sudo systemctl disable --now alloy
kubectl --context brownrook-k3s1 -n home-budget \
  delete secret external-log-reader-token
```

Removing the service account, Role, and RoleBinding is a separate Git change.
Loki and Grafana remain available through Method A, and Kubernetes container
logs remain available through `kubectl logs`.

To disable Method A without changing application workloads, change
`brownrook-infra/kubernetes/gitops/apps/ledger-app.yaml` back to
`deploy/rabbitmq-private` and merge that reviewed infrastructure change. After
the parent reconciles the child Application, sync it and remove the TLS Secret
only after the DaemonSet is gone:

```sh
argocd app wait brownrook-root --sync --health --timeout 600 --grpc-web
argocd app get ledger --grpc-web -o json \
  | jq -e '.spec.source.path == "deploy/rabbitmq-private"'
argocd app sync ledger --grpc-web
argocd app wait ledger --sync --health --timeout 600 --grpc-web
kubectl --context brownrook-k3s1 -n home-budget \
  delete secret fluent-bit-loki-tls
```

The persisted node buffer remains at `/var/lib/home-budget/fluent-bit` for
forensic recovery and must be removed separately on each node if retention is
not required. Rotate a Fluent Bit client identity by updating the Secret and
restarting the DaemonSet. Keep Loki mTLS enabled while its LAN listener is
active, even after Method A is disabled.
