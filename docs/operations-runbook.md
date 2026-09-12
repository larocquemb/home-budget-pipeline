# BrownRook Ledger operations runbook

This runbook covers the normal build, GitOps deployment, database migration,
and receipt-processing verification workflow.

## Local setup and tests

Activate the project environment:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev,db]'
```

Application commands use `DATABASE_URL`, then `HOME_BUDGET_PG_DSN` where the
command supports that fallback. Tests use the isolated `home_budget_test`
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

Validate database setup locally:

```bash
brownrook database setup
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
| `make test` | Run unit tests without external-service integration tests. |
| `make test-db-setup` | Create the disposable test database if needed, then rebuild its schemas. |
| `make test-db` | Rebuild the disposable test schemas and run integration tests. |
| `make test-db-verbose` | Run database setup and integration tests with full PostgreSQL output. |
| `make test-all` | Run unit tests followed by integration tests. |
| `make test-receipts` | Copy real receipt inputs into an isolated workspace and process them against the disposable test database. |
| `make dev-up` | Start the local supporting services from `compose.dev.yaml`. |
| `make dev-down` | Stop the local supporting services. |
| `make dev-web` | Run the Ledger web application against the local services. |
| `make dev-cert-install` | Install the local Caddy certificate authority in the macOS system keychain. |
| `make dev-db-reset` | Rebuild the local development database configured in `.env.dev`. |
| `make status` | Report deployment and recent workflow status. |
| `make enrich-products` | Start and follow a one-off Kubernetes product-enrichment job. |

The `test-db*` targets destroy and recreate schemas only in `TEST_DB`, which
defaults to `home_budget_test`. `make test-receipts` also recreates
`RECEIPT_TEST_ROOT`, which defaults to `.receipt-test`.

## Commit and push

```bash
git status
git add <changed-files>
git commit -m "Describe the change"
git pull --rebase origin main
git push origin main
```

CI writes a deployment commit back to `main` after publishing an immutable
image tag. A push can therefore be rejected if that deployment commit arrived
first. Use `git pull --rebase origin main` and push again. Never force-push
`main` to resolve this race.

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

The `ledger` application tracks `main` with automated sync, pruning, and
self-healing. A PreSync hook runs `home-budget-db-setup` before workloads are
updated. The hook applies current constraint policy and receipt-schema updates.
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
argocd app sync ledger
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

1. Extraction audit
2. Review queue
3. Expenses and evidence
4. Receipt processing
5. Duplicates
6. Category spend and transactions

The detailed workflow and status definitions are in [the user guide](user-guide.md).
