# Ledger public and private authentication

Public URL: `https://idc.brownrook.com/ledger`

Corporate-LAN URL: `https://ledger.brownrook.net/ledger`

The existing BrownRook NGINX proxy terminates TLS for `idc.brownrook.com` and
forwards requests to the K3s ingress endpoint. The Kubernetes HTTP ingress
routes only `/ledger` to `ledger-oauth2-proxy`, which authenticates the request
before proxying it to `ledger-web`.

## Entra ID application

Create a single-tenant Microsoft Entra app registration for Ledger.

Configure the Web redirect URI:

`https://idc.brownrook.com/ledger/oauth2/callback`

Recommended access control:

- Keep the app single-tenant.
- In the Enterprise Application, require assignment and assign only approved Ledger users/groups.
- Require MFA/Conditional Access where licensed and appropriate.

Create the Kubernetes OAuth secret from `oauth2-proxy-secret.example.yaml`. Never commit the populated secret.

Generate the cookie secret with:

```bash
openssl rand -base64 32 | tr -- '+/' '-_'
```

## PostgreSQL

Create `postgres-secret.yaml` from `postgres-secret.example.yaml`, using a strong password. The StatefulSet uses the K3s `local-path` storage class and requests 10 GiB.

The in-cluster database host is:

`postgres.home-budget.svc.cluster.local:5432`

The PostgreSQL Service is ClusterIP. `postgres-ingressroutetcp.yaml` also binds
it to Traefik's dedicated `postgres` TCP entry point for private administrative
access; that entry point must not be published to the public internet.

## Apply

Create the populated `postgres-secret`, `ledger-oauth2-proxy-secret`,
`pve-smb-credentials`, and `ghcr-secret` resources outside Git. Then apply the
Git-managed resources:

```bash
kubectl apply -k k8s
```

`ledger-web` serves the receipt-first application on port 8080. OAuth2 Proxy
passes the authenticated Entra identity headers to Ledger. Kubernetes calls
`/ledger/health` and `/ledger/ready` directly for probes. Public `/ledger`
requests still pass through OAuth2 Proxy.

## Private corporate route

The optional private overlays add `https://ledger.brownrook.net/ledger` on the
existing Traefik `websecure` entry point at `192.168.2.240`. They run a separate
OAuth2 Proxy so the private hostname has its own callback URL and secure cookie
while retaining the same Entra tenant and access policy.

Add this Web redirect URI to the Entra application:

`https://ledger.brownrook.net/ledger/oauth2/callback`

The private hostname must resolve only through corporate DNS. The public reverse
proxy must forward only explicitly configured public hosts and must not forward
the private `.net` hostname. Create the `k3ingress-tls` secret outside
Git before using `deploy/private-lan` or `deploy/rabbitmq-private`. See the
[network access guide](private-networking.md) for the required DNS, TLS, and
firewall boundaries.
