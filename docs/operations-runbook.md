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
home-budget-db-setup
```

The container sets `HOME_BUDGET_SQL_DIR=/opt/app-root/src/sql`; local execution
uses the repository `sql/` directory.

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
