# BrownRook network access

BrownRook uses separate access paths for local development, public Ledger
access, private corporate access, and service-to-service traffic. Authentication
is required on both public and private browser routes.

## Network boundaries

**Mac development**

Address: `https://ledger-dev.brownrook.net/ledger`

The name resolves to `127.0.0.1`. Caddy listens only on loopback and sends users
through Microsoft Entra ID.

**Public K3s**

Address: `https://idc.brownrook.com/ledger`

The public edge terminates TLS and sends users through Microsoft Entra ID using
the public OAuth2 Proxy.

**Corporate K3s**

Address pattern: `*.brownrook.net`

Private DNS serves `192.168.2.0/24`. Traefik listens at `192.168.2.240`; private
TLS and authentication protect browser services.

**Kubernetes services**

Address pattern: `*.home-budget.svc.cluster.local`

These names carry cluster-internal application, PostgreSQL, and RabbitMQ
traffic.

Private DNS is one part of the access boundary. Traefik's LoadBalancer address
is private, and the public reverse proxy must forward only explicitly configured
public hostnames. Do not publish the `.brownrook.net` application hostnames in
public DNS or forward them through the public proxy. Do not publish PostgreSQL
or AMQP ports on the public edge.

## Mac development

`compose.dev.yaml` publishes Caddy on `127.0.0.1:80` and `127.0.0.1:443`.
`compose.queue-test.yaml` similarly publishes its disposable RabbitMQ ports only
on loopback. Other LAN devices cannot connect to those published ports.

Ledger itself listens on port 8080 so the OAuth2 Proxy container can reach the
host through `host.docker.internal`. A separate `LEDGER_PROXY_SECRET` protects
the authenticated identity headers on that hop. Caddy adds the secret after
OAuth2 Proxy authentication, and Ledger rejects requests missing the configured
secret. Health and readiness endpoints contain no application data and remain
available for probes.

Generate a value independently from the OAuth cookie secret and put it in the
uncommitted `.env.dev` file:

```bash
openssl rand -hex 32
```

Keep `ledger-dev.brownrook.net` mapped to `127.0.0.1`, either in `/etc/hosts` or
in DNS used only by the Mac. See the
[README local setup](https://github.com/larocquemb/home-budget-pipeline#15-local-entra-authenticated-ledger)
for the complete startup sequence.

## Private K3s prerequisites

The private overlays follow the established Argo CD pattern: they use Traefik's
existing `websecure` entry point on the private LoadBalancer address
`192.168.2.240`. Public routes use the same entry point after an explicitly
configured public reverse proxy forwards their hostnames. Keep the new `.net`
hostnames absent from that public proxy.

Create these records in private DNS:

| Name | Address | Use |
| --- | --- | --- |
| `ledger.brownrook.net` | `192.168.2.240` | Authenticated Ledger web application |
| `rabbitmq.brownrook.net` | `192.168.2.240` | Authenticated RabbitMQ management UI |
| `postgres.brownrook.net` | `192.168.2.240` | Optional private PostgreSQL administrative endpoint |

Create valid TLS secrets named `ledger-brownrook-net-tls` and
`rabbitmq-brownrook-net-tls` in the `home-budget` namespace. Keep certificates,
private keys, and populated Kubernetes secrets outside Git.

The private Ledger route uses a second OAuth2 Proxy deployment because its
callback host and secure cookie differ from the public `.com` route. Add this
redirect URI to the existing Entra application:

`https://ledger.brownrook.net/ledger/oauth2/callback`

The private proxy reuses `ledger-oauth2-proxy-secret`, so the same tenant,
client, assignment policy, MFA, and Conditional Access policy apply to both
routes.

## Select an overlay

Use one deployment path at a time:

| Overlay | Contents |
| --- | --- |
| `k8s` | Base application with the public Ledger route. |
| `deploy/private-lan` | Base application plus the authenticated private Ledger route. |
| `deploy/rabbitmq` | Base application plus queued receipt processing; no private browser ingress. |
| `deploy/rabbitmq-private` | Queued processing plus private Ledger and RabbitMQ management routes. |

Render the desired overlay before changing the GitOps application path:

```bash
kubectl kustomize deploy/private-lan
kubectl kustomize deploy/rabbitmq-private
```

## Database and broker access

Application workloads use the cluster DNS names
`postgres.home-budget.svc.cluster.local:5432` and
`rabbitmq.home-budget.svc.cluster.local:5672`.

The RabbitMQ private ingress exposes only its HTTPS management UI through the
existing `websecure` entry point at
`https://rabbitmq.brownrook.net`. RabbitMQ's own named user and strong password
remain required. AMQP port 5672 stays inside the cluster.

PostgreSQL remains a ClusterIP service. The existing `postgres` Traefik TCP
entry point is for private administration and must bind only to a private
address with firewall restrictions. It passes PostgreSQL traffic through and
does not add database TLS. Prefer a local `kubectl port-forward` when permanent
LAN access is unnecessary. Require PostgreSQL credentials in either case; add
PostgreSQL TLS before enabling a permanent LAN endpoint.
