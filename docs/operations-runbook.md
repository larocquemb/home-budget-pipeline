# BrownRook Ledger operations runbook

This runbook covers the normal build, GitOps deployment, database migration,
and receipt-processing verification workflow.

## Local setup and tests

Activate the project environment:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev,db]'
```

The install creates the primary `ledger` command. The former `brownrook`
command remains a compatibility alias. Re-run the editable install after
pulling a CLI-entry-point change so the virtual environment creates the new
executable.

Application commands accept `DATABASE_URL` and, where documented by `--help`,
`HOME_BUDGET_PG_DSN`; use `DATABASE_URL` consistently in shared environments.
Tests use the isolated `home_budget_test`
database and must not point at the normal `home_budget` database because test
setup drops and rebuilds schemas.

Run unit tests:

```bash
make test
```

Run database integration tests:

```bash
make test-db
```

The Make integration targets use `TEST_RABBITMQ_URL` when explicitly set and
otherwise fall back to `RABBITMQ_URL`. RabbitMQ integration tests skip when
neither variable is exported. With the local development environment loaded,
include them with:

```bash
set -a
source .env.dev
set +a
make test-db
```

The tests use unique `test.<uuid>` queues and delete them afterward. Set an
explicit `TEST_RABBITMQ_URL` when the test broker differs from the development
broker.

Initialize or upgrade the runtime database locally:

```bash
ledger database setup
```

The container sets `HOME_BUDGET_SQL_DIR=/opt/app-root/src/sql`; local execution
uses the repository `sql/` directory.

The Dockerfile installs third-party dependencies before application source is
copied. Source-only builds therefore reuse the dependency layer; changes to
`pyproject.toml` intentionally invalidate it.

## Make targets

Run `make help` for a compact list. The repository provides these targets:

| Target | Purpose |
| --- | --- |
| `make docs-build` | Build the online manual locally and fail on documentation warnings. |
| `make docs-serve` | Serve the online manual locally with live reload. |
| `make test` | Run unit tests without external-service integration tests. |
| `make test-db-setup` | Create the disposable test database if needed, then rebuild its schemas. |
| `make test-db` | Rebuild the disposable test schemas and run integration tests, including RabbitMQ when a broker URL is exported. |
| `make test-db-verbose` | Run database setup and integration tests with full PostgreSQL output. |
| `make test-rabbit` | Rebuild the disposable test schemas, then run the five RabbitMQ integration tests with their names displayed. |
| `make test-all` | Run unit tests followed by PostgreSQL and available RabbitMQ integration tests; the integration summary displays their names. |
| `make test-receipts` | Copy real receipt inputs into an isolated workspace and process them against the disposable test database. |
| `make dev-up` | Start the local supporting services from `compose.dev.yaml`. |
| `make dev-down` | Stop the local supporting services. |
| `make dev-web` | Run the Ledger web application against the local services. |
| `make dev-cert-install` | Install the local Caddy certificate authority in the macOS system keychain. |
| `make dev-db-reset` | Rebuild the local development database configured in `.env.dev`. |
| `make status` | Report deployment and recent workflow status. |
| `make enrich-products` | Start and follow a one-off Kubernetes product-enrichment job. |
| `make deploy-k3s` | Apply PostgreSQL credentials and the OCR cache path from `.env.k3s`, then sync the Argo CD application. Matching settings are reused. |
| `make k3s-config-check` | Validate the K3s OCR cache setting in `.env.k3s` without cluster access. |
| `make k3s-config-apply` | Update `receipt-runtime-config` from `.env.k3s` and restart Deployment clients when the cache path changes. |
| `make postgres-config-check` | Validate PostgreSQL deployment settings in `.env.k3s` without cluster access. |
| `make postgres-config-apply` | Provision `postgres-secret` from `.env.k3s` for a new deployment. |
| `make postgres-password-rotate` | Rotate an existing database password and Secret, verify authentication, and restart database clients. |

The `test-db*` targets destroy and recreate schemas only in `TEST_DB`, which
defaults to `home_budget_test`. `make test-receipts` also recreates
`RECEIPT_TEST_ROOT`, which defaults to `.receipt-test`.

Install the documentation dependencies with
`python -m pip install -e '.[docs]'`. Pull requests build the manual with strict
validation. Merges to `main` publish it to GitHub Pages at
`https://larocquemb.github.io/home-budget-pipeline/`.

For private-LAN deployments, select either `deploy/private-lan` or
`deploy/rabbitmq-private` after satisfying the DNS, TLS, Entra callback,
Traefik, and firewall prerequisites in the
[network access guide](private-networking.md).

## Commit and pull request

```bash
git status
git switch -c <short-branch-name>
git add <changed-files>
git commit -m "Describe the change"
git push -u origin HEAD
gh pr create --base main
gh pr checks --watch
gh pr merge --squash --delete-branch
```

After a merge, CI writes a deployment commit to `main` after publishing an
immutable image tag. Start each branch from an updated `main`; if the deployment
commit arrives first, update the branch from `origin/main` before merging.
Never force-push `main` to resolve this race.

### Documentation-only change

Start from synchronized `main`, build the manual with strict validation, and
review the staged change before creating a pull request:

```bash
git switch main
git pull --ff-only
git switch -c docs/<short-topic>

make docs-build

git add docs/ README.md mkdocs.yml
git diff --cached --check
git commit -m "Describe the documentation change"
git push -u origin HEAD

gh pr create --base main --fill
gh pr checks --watch
gh pr merge --squash --delete-branch

git switch main
git pull --ff-only
```

Stage only files that belong to the change; `git add docs/ README.md mkdocs.yml`
is appropriate when all current documentation edits are intentional. Pull
requests validate the manual. After merge, the Documentation workflow publishes
GitHub Pages and the CI workflow validates the repository on `main`.

## GitHub Actions and GHCR

List recent workflows:

```bash
gh run list --limit 5
```

Watch a run that is currently in progress:

```bash
gh run watch --exit-status
```

If no run is in progress, `gh run watch` reports that directly; use
`gh run list` to confirm the latest completed result. A successful main-branch
workflow tests the project, builds the amd64 image, pushes commit and `main`
tags to GHCR, publishes the same image under the stable PostgreSQL bootstrap
alias, and updates `k8s/kustomization.yaml` in a deployment commit.

The PostgreSQL init container uses
`ghcr.io/larocquemb/home-budget-schema-bootstrap:main`. This name is separate
from the Kustomize-managed application image, so ordinary application releases
do not change the StatefulSet pod template or restart PostgreSQL. A fresh
PostgreSQL volume still receives the latest bootstrap files. The versioned
PreSync migration image remains tied to the application deployment.

## Argo CD deployment

Argo CD owns application deployments and workload restarts. `make deploy-k3s`
provisions the local environment's PostgreSQL Secret and OCR ConfigMap, then
requests an Argo CD sync. Configuration changes that need fresh pods use Argo CD
Deployment restart actions. The helpers do not apply workload manifests or run
`kubectl rollout restart`. They require an Argo CD login and permission to run
the application's restart actions. `ARGO_APP` and `ARGO_SERVER` configure both
sync and restart operations.

Open the private Argo CD UI at
`https://argocd.brownrook.net/applications/ledger`. For CLI operations, log in
through the same private hostname:

```bash
argocd login argocd.brownrook.net --grpc-web
argocd account get-user-info
```

The current Argo CD installation uses native Argo CD accounts; SSO is not
configured. The login command prompts for the username and password, then stores
its session in the user's Argo CD configuration. Do not put an Argo CD password
or token in the repository or shell history.

Show recent CI runs, Argo status, Kubernetes Jobs, and Pods in one read-only
dashboard:

```bash
make status
```

The underlying script is `scripts/deployment_status.sh`. Its defaults can be
overridden when needed:

```bash
RUN_LIMIT=10 ARGO_APP=ledger KUBE_NAMESPACE=home-budget make status
```

`make status` does not require an Argo CD login. It reads the Application in
core mode through the current Kubernetes context and falls back to `kubectl`.
It does require working GitHub CLI authentication, Kubernetes access, and
`jq`. When multiple kubeconfigs exist, set
`KUBECONFIG=~/.kube/config-brownrook` first.

The `ledger` application tracks `main`. Its current sync policy is manual:
merging a PR and publishing an image does not deploy it until Argo CD is synced.
Use `make deploy-k3s` to provision environment configuration and request the sync,
or use the Argo CD UI/CLI when configuration is already provisioned.
A PreSync hook runs `home-budget-db-setup` before workloads are
updated. The hook applies current constraints, audit and description fields,
merchant aliases, OCR learning, product enrichment, and blue/green receipt
processing state.
If it fails, Argo correctly leaves application workloads OutOfSync.

Check status:

```bash
argocd app get ledger --refresh
kubectl -n argocd get application ledger
kubectl -n home-budget get jobs,pods
```

To refresh repository state and retry a failed sync:

```bash
argocd app get ledger --hard-refresh
argocd app sync ledger --grpc-web
argocd app wait ledger --sync --health --timeout 600 --grpc-web
make status
```

Do not pass a commit with `--revision` while the application is configured to
track `main`. If Argo reports another operation in progress, inspect it first.
Terminate it only when it is genuinely stuck:

```bash
argocd app terminate-op ledger
```

## Capturing PreSync hook failures

Argo may remove hook Jobs and pods quickly. Start a watcher before retrying:

```bash
while true; do
  pod=$(kubectl -n home-budget get pods \
    -l app=receipt-processing-schema-upgrade \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  [ -n "$pod" ] && kubectl -n home-budget logs -f "$pod" && break
  sleep 1
done
```

Run `argocd app sync ledger` in another terminal. A successful hook may be
deleted automatically after completion.

## Verify the rollout

```bash
kubectl -n home-budget rollout status deployment/ledger-web
kubectl -n home-budget get pods
argocd app get ledger
```

Old failed receipt-processor pods may remain because the CronJob retains failed
history. Judge the current state using the newest Job and its logs.

## Receipt processor verification

The CronJob runs every 15 minutes. Find and inspect the newest scheduled Job:

```bash
latest=$(kubectl -n home-budget get jobs \
  -l app=receipt-processor \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}')
kubectl -n home-budget logs "job/$latest"
```

For an immediate smoke test:

```bash
kubectl -n home-budget create job \
  --from=cronjob/receipt-processor receipt-processor-test
kubectl -n home-budget logs -f job/receipt-processor-test
```

Success requires `failed: 0`. `review_required` is a data-quality outcome, not
an infrastructure failure; inspect those receipts in the web review reports.

## Web and data-quality verification

After rollout, use the dashboard in this order:

1. Receipts
2. Extraction audit
3. Review queue
4. Expenses and evidence
5. Category rules
6. Receipt processing
7. Duplicates
8. Category spend and transactions

The detailed workflow and status definitions are in [the user guide](user-guide.md).
