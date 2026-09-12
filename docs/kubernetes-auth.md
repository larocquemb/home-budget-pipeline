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

Specify the deployment password in the Git-ignored `.env.k3s`:

```dotenv
POSTGRES_DB=home_budget
POSTGRES_USER=home_budget
POSTGRES_PASSWORD='your-chosen-password'
POSTGRES_HOST=postgres.brownrook.net
POSTGRES_PORT=5432
DATABASE_URL='postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}'
HOME_BUDGET_PG_DSN='${DATABASE_URL}'
```

Deploy the existing Argo CD application with:

```bash
make deploy-k3s
```

This validates `.env.k3s`, checks Argo CD access, synchronizes `postgres-secret`
and the database password, applies the OCR cache setting to `receipt-runtime-config`,
then starts the Argo CD deployment. Set `HOME_BUDGET_OCR_CACHE` in `.env.k3s`
to a directory below `/data`; see the [cache configuration guide](receipt-processing.md#manual-run).
If the password
already matches, it is reused without password changes or client restarts.
When you specify a different password, the command updates the existing database
and its clients before deploying. The job-suspension requirements below apply
only when changing the password. Other existing Secrets, including the PV's SMB
credentials, remain in Kubernetes. The Argo application and its namespace must
already exist; use the initial provisioning commands below for a new installation.

The command uses the existing Argo CD login described in the
[operations runbook](operations-runbook.md#argo-cd-deployment). Argo CD still
deploys its configured Git revision; it does not deploy uncommitted local files.
For automated GitOps deployments, the provisioned Secret supplies the password;
Argo CD does not read your Mac's `.env.k3s`.

Set `POSTGRES_DB`, `POSTGRES_USER`, and your chosen `POSTGRES_PASSWORD` in
`.env.k3s`. Quote values containing spaces or `#`. The deployment reader expands
`${POSTGRES_*}` references in the two URL entries, URL-encoding username,
password, and database name. `HOME_BUDGET_PG_DSN` can reference `${DATABASE_URL}`.
Other settings, including passwords, are literal; shell commands are never
executed. Keep the URL templates single-quoted as shown and use the deployment
command to resolve them, rather than sourcing them in a shell. The StatefulSet uses the K3s `local-path` storage class
and requests 10 GiB.

For a new deployment, create the namespace and provision its Secret before
applying the application manifests:

```bash
make postgres-config-check
kubectl --context brownrook-k3s1 apply -f k8s/namespace.yaml
make postgres-config-apply
make k3s-config-apply
```

The command reads `KUBE_CONTEXT`, `KUBE_NAMESPACE`, `DATABASE_URL`, and
`HOME_BUDGET_PG_DSN` from `.env.k3s`. URLs may be templates as above or complete
PostgreSQL URLs with URL-encoded credentials. Their credentials and database
must match the `POSTGRES_*` values; no hostname is substituted by the script. It
sends only PostgreSQL settings to `postgres-secret`, without displaying them or
writing a populated manifest to disk. `k8s/postgres-secret.example.yaml` remains
available as a manual template.
Changing only a connection URL updates the Secret and restarts its Deployment
clients without changing the database password.

For the existing database, edit `POSTGRES_PASSWORD` in `.env.k3s`, suspend any
database-consuming CronJobs, let their active jobs finish, then run:

```bash
make postgres-config-check
make postgres-password-rotate
```

Rotation changes the database role password, updates the Secret, verifies a new
authenticated connection, and asks Argo CD to restart Deployments that consume
`postgres-secret`. The helpers verify that Argo CD manages the affected
Deployments before changing configuration. `ARGO_APP` and `ARGO_SERVER` select
the same application for deployment sync and restart actions.
It uses a temporary loopback port-forward and requires the project's database
dependencies (`.[db]`). It refuses to rename the database/user or run while
database CronJobs are unsuspended or Jobs are unfinished. The existing database
role must be able to read its password verifier from `pg_authid`, as the current
bootstrap role can. If a rejected Secret update is confirmed by reading it back,
the command restores the old database password. A connectivity loss during
rotation may require checking database/Secret consistency before retrying.

Check client rollout status before restoring the intended CronJob schedules.
Existing database connections may briefly fail while clients reload credentials.
PostgreSQL itself is not restarted. A Secret-only update cannot change the
password in an initialized database; `postgres-config-apply` refuses a password
change and directs you to the rotation command instead. See PostgreSQL's
[ALTER ROLE documentation](https://www.postgresql.org/docs/17/sql-alterrole.html)
and Kubernetes' [Secret environment variable behavior](https://kubernetes.io/docs/tasks/inject-data-application/distribute-credentials-secure/#define-container-environment-variables-using-secret-data).

The in-cluster database host is:

`postgres.home-budget.svc.cluster.local:5432`

The PostgreSQL Service is ClusterIP. `postgres-ingressroutetcp.yaml` also binds
it to Traefik's dedicated `postgres` TCP entry point for private administrative
access; that entry point must not be published to the public internet.

## Apply

Keep deployment settings in the Git-ignored `.env.k3s`, using
`.env.k3s.example` as the checklist. `.env.dev` is only for the local Mac.
The Entra tenant and app credentials can be shared: map local `ENTRA_CLIENT_ID`
and `ENTRA_CLIENT_SECRET` to `OAUTH2_PROXY_CLIENT_ID` and
`OAUTH2_PROXY_CLIENT_SECRET`. Use the existing cluster cookie secret rather than
the Mac's `OAUTH2_COOKIE_SECRET`.

The deployment also needs PostgreSQL credentials, SMB credentials,
and GHCR pull token. Product enrichment uses `BRAVE_SEARCH_API_KEY` in
`brave-search-api` and `OPENAI_API_KEY` in `openai-api`. Use the in-cluster
PostgreSQL hostname above when constructing database URLs; Mac receipt paths,
test broker URLs, and `LEDGER_PROXY_SECRET` are local-only settings for the
current manifests. TLS secrets remain managed separately.

Kubernetes does not load `.env.k3s` automatically. Transfer the values into the
corresponding Secrets before deployment. For RabbitMQ, the
[receipt processing runbook](rabbitmq-receipts.md#kubernetes-rollout) includes a
command that creates its Secret from `.env.k3s`.

Create the populated `postgres-secret`, `ledger-oauth2-proxy-secret`,
`pve-smb-credentials`, and `ghcr-secret` resources outside Git. Configure the
Argo CD application's source path for the desired overlay, then deploy through
Argo CD:

```bash
make deploy-k3s
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
