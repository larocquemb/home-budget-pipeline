# Ledger public authentication

External URL: `https://idc.brownrook.com/ledger`

The existing BrownRook NGINX proxy terminates TLS for `idc.brownrook.com` and forwards requests to the K3s ingress endpoint. The Kubernetes ingress routes only `/ledger` to `ledger-oauth2-proxy`. PostgreSQL remains ClusterIP-only and is never exposed publicly.

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

PostgreSQL is intentionally exposed only through a ClusterIP Service.

## Apply

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/postgres-secret.yaml
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/oauth2-proxy-secret.yaml
kubectl apply -f k8s/ledger-web-service.yaml
kubectl apply -f k8s/oauth2-proxy.yaml
kubectl apply -f k8s/ledger-ingress.yaml
```

`ledger-web` is currently the service contract for the future Ledger HTTP/API workload on port 8080. Until a pod with label `app: ledger-web` exists, authentication can complete but the authenticated upstream will return an unavailable response.
