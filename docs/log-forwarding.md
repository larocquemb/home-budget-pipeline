# Kubernetes log forwarding

Method B is the initial production path:

```text
home-budget Pods -> Kubernetes API (TLS) -> external Alloy -> Loki (TLS) -> Grafana
```

Grafana Alloy runs as a systemd service on `monitoring.idc.brownrook.net`, not in
K3s. It discovers and tails only Pods in the `home-budget` namespace through the
Kubernetes API and writes them to the existing Loki instance on the monitoring
host. This proves the constrained external-collector model before any node-level
Fluent Bit agent is installed.

The later Method A path remains available but is not part of this rollout:

```text
home-budget Pods -> Fluent Bit DaemonSet -> Loki -> Grafana
```

## Develop and test Method B locally

Use the disposable local environment before changing production K3s. It runs
entirely on the Mac through the active Docker/Colima backend:

```text
home-budget in Kind -> Kind API (TLS) -> external Alloy container
                                           |
                                           v
                                  local Loki (TLS) -> local Grafana
```

All Kubernetes commands in the local scripts explicitly use the
`kind-home-budget-logging` context. They do not read from or deploy to
`brownrook-k3s1`. Alloy joins Kind's Docker network but is not deployed inside
the cluster, preserving the production Method B trust boundary.

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
application image, loads it into Kind, deploys the namespace-scoped reader,
generates a disposable 30-day CA and Loki server certificate, exports a local
restricted kubeconfig, and starts Alloy, Loki, and Grafana:

```sh
make dev-logging-up
```

Run the end-to-end test:

```sh
make dev-logging-test
```

The test sends a uniquely named request to the application, confirms that the
request is in the `home-budget-local` Pod's Uvicorn log, and queries Loki over
verified HTTPS for the same marker. A passing run prints the exact Grafana
LogQL query.

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

No configuration enables `insecure_skip_verify`. Loki remains bound to loopback;
port 3100 is not exposed to the LAN. The certificate must include
`monitoring.idc.brownrook.net` in its SANs and be supplied as a full chain.

The API credential is a manually provisioned service-account token Secret. This
long-lived credential supports an always-running external collector but must be
protected and rotated. The populated Secret, exported kubeconfig, private key,
and populated certificates stay outside Git. Delete the token Secret to revoke
the collector immediately.

Alloy promotes only the bounded `cluster`, `environment`, `collection`,
`namespace`, `pod`, `container`, `node`, `app`, and `job` fields to Loki labels.
It does not copy arbitrary Pod labels or annotations. A processing stage redacts
common PostgreSQL URL, API key, token, password, and Bearer-token forms. This is
defense in depth; applications must still avoid logging credentials and personal
data.

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

## Prepare TLS on the monitoring host

Issue the dedicated leaf certificate from the Brown Rook intermediate CA. Its
SANs should include `monitoring.idc.brownrook.net` and
`grafana.idc.brownrook.net`. Keep `monitoring.key` on the monitoring host and
copy only the signed full chain back to it.

Copy the public root CA, full chain, external kubeconfig, and Alloy configuration
from the Mac:

```sh
scp ~/brownrook-ca/root/root_ca.crt \
  ~/brownrook-ca/leafs/monitoring/monitoring.fullchain.crt \
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
sudo update-ca-certificates
```

Merge the `server` settings from
[`loki-tls-config.example.yaml`](https://github.com/larocquemb/home-budget-pipeline/blob/main/deploy/external-logging/loki-tls-config.example.yaml)
into the existing `/etc/loki/config.yml`; do not replace its storage, schema, or
retention configuration. Back up and edit the file:

```sh
sudo cp -a /etc/loki/config.yml \
  "/etc/loki/config.yml.backup-$(date +%Y%m%d-%H%M%S)"
sudoedit /etc/loki/config.yml
```

The resulting `server` section must retain any existing settings while adding:

```yaml
server:
  http_listen_address: 127.0.0.1
  http_listen_port: 3100
  tls_min_version: VersionTLS12
  http_tls_config:
    cert_file: /etc/monitoring/tls/monitoring.fullchain.crt
    key_file: /etc/monitoring/tls/monitoring.key
```

Validate and restart Loki:

```sh
sudo -u loki /usr/bin/loki \
  -config.file=/etc/loki/config.yml -verify-config
sudo systemctl restart loki
sudo systemctl --no-pager --full status loki

curl --cacert /tmp/root_ca.crt \
  --resolve monitoring.idc.brownrook.net:3100:127.0.0.1 \
  https://monitoring.idc.brownrook.net:3100/ready
```

Update the Grafana Loki data-source URL to
`https://monitoring.idc.brownrook.net:3100`. Because Loki listens only on
loopback, add this host-local resolution while retaining normal LAN DNS for
other clients:

```text
127.0.0.1 monitoring.idc.brownrook.net
```

Grafana uses the system trust store updated above to validate the Brown Rook CA.

## Install and configure external Alloy

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
sudo install -m 0640 -o root -g alloy \
  /tmp/config.alloy /etc/alloy/config.alloy
sudo chown -R alloy:alloy /var/lib/alloy
sudo chmod 0750 /var/lib/alloy

sudo -u alloy /usr/bin/alloy validate /etc/alloy/config.alloy
sudo systemctl enable --now alloy
sudo systemctl --no-pager --full status alloy
sudo journalctl -u alloy -n 100 --no-pager
```

Remove the temporary credential and certificate copies after both services are
healthy:

```sh
rm -f /tmp/home-budget.kubeconfig /tmp/config.alloy
rm -f /tmp/root_ca.crt /tmp/monitoring.fullchain.crt
```

## Prove end-to-end collection

Create a short-lived, non-sensitive test log from the Mac:

```sh
PROBE="method-b-probe-$(date +%s)"
kubectl --context brownrook-k3s1 -n home-budget run "$PROBE" \
  --image=busybox:1.37 --restart=Never -- \
  sh -c "echo METHOD_B_PROBE_${PROBE}; sleep 15"
kubectl --context brownrook-k3s1 -n home-budget wait \
  --for=condition=Ready "pod/$PROBE" --timeout=60s
```

On the monitoring host, query Loki through verified TLS:

```sh
curl --fail --silent --show-error --get \
  --cacert /etc/alloy/brown-rook-root-ca.crt \
  --resolve monitoring.idc.brownrook.net:3100:127.0.0.1 \
  --data-urlencode 'query={cluster="brownrook-k3s1",namespace="home-budget"} |= "METHOD_B_PROBE"' \
  --data-urlencode 'limit=20' \
  https://monitoring.idc.brownrook.net:3100/loki/api/v1/query_range
```

The same LogQL query in Grafana Explore must return the probe line with its
`namespace`, `pod`, `container`, `node`, `job`, and collection labels. Delete the
probe Pod after verification.

## Rotation and rollback

To rotate the Kubernetes credential, delete and recreate the token Secret,
export a new kubeconfig with `FORCE=1`, install it on the monitoring host, and
restart Alloy. To revoke Method B immediately:

```sh
sudo systemctl disable --now alloy
kubectl --context brownrook-k3s1 -n home-budget \
  delete secret external-log-reader-token
```

Removing the service account, Role, and RoleBinding is a separate Git change.
Loki and Grafana remain available, and Kubernetes container logs remain
available through `kubectl logs`.

Method A is deferred. Do not change the Argo CD application to
`deploy/rabbitmq-logging` or install the Fluent Bit DaemonSet during this rollout.
