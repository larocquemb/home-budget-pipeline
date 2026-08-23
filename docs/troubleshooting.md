# BrownRook Ledger troubleshooting guide

This guide records recovery procedures for the Kubernetes, Argo CD, PostgreSQL,
containerd, persistent-storage, and receipt-processing failures observed in the
`home-budget` deployment.

Use the operations runbook for the normal deployment workflow. Use this guide
when the cluster is `Unknown`, `Degraded`, `OutOfSync`, or a workload cannot
start.

## 1. Make sure kubectl is using k3s

The workstation may also contain an EKS context. An expired AWS token can make a
healthy k3s cluster look inaccessible:

```text
ExpiredToken ... AssumeRole ... security token included in the request is expired
```

Select the BrownRook kubeconfig before troubleshooting:

```bash
export KUBECONFIG=~/.kube/config-brownrook
kubectl config current-context
kubectl -n home-budget get pods
```

For a single command:

```bash
KUBECONFIG=~/.kube/config-brownrook kubectl -n home-budget get pods
```

## 2. Argo CD reports ComparisonError / Sync Unknown

Typical error:

```text
ComparisonError: Failed to load target state ...
dial tcp <repo-server-service-ip>:8081: connect: connection refused
```

Check the repo server:

```bash
kubectl -n argocd get pods \
  -l app.kubernetes.io/name=argocd-repo-server -o wide
```

If the repo-server pod is `Unknown`, `0/1`, or stuck in an init-container
restart loop, recreate the pod:

```bash
kubectl -n argocd delete pod \
  -l app.kubernetes.io/name=argocd-repo-server

kubectl -n argocd get pods \
  -l app.kubernetes.io/name=argocd-repo-server -w
```

Wait for `1/1 Running`, then force Argo to compare the repository again:

```bash
kubectl -n argocd annotate application ledger \
  argocd.argoproj.io/refresh=hard --overwrite

argocd app get ledger --core
```

A stale `SyncError` condition may remain from the failed attempt even after
repository comparison starts working. Use the current resource table and
`Sync Status` to determine the active problem.

## 3. Argo sync is waiting for the schema PreSync hook

The deployment uses the `receipt-processing-schema-upgrade` PreSync Job. If an
operation stays `Running`, inspect it:

```bash
kubectl -n argocd get application ledger \
  -o jsonpath='{.status.operationState.phase}{"\n"}{.status.operationState.message}{"\n"}'

kubectl -n home-budget get jobs,pods \
  -l app=receipt-processing-schema-upgrade
```

Argo may automatically remove a successful hook. `No resources found` after a
successful run can therefore be normal.

### Advisory-lock wait

If the hook is running but making no progress, inspect PostgreSQL sessions:

```sql
SELECT
  pid,
  usename,
  state,
  wait_event_type,
  wait_event,
  pg_blocking_pids(pid) AS blocking_pids,
  now() - query_start AS age,
  left(query, 160) AS query
FROM pg_stat_activity
ORDER BY query_start;
```

A session waiting on:

```text
SELECT pg_advisory_lock(hashtext($1))
```

is waiting for another process that holds the migration/processing advisory
lock. Identify the blocking PID before killing or restarting anything.

## 4. ImagePullBackOff with `blob not found`

A particularly misleading failure looks like this:

```text
failed to extract layer ...
failed to get reader from content store:
blob sha256:<digest> expected at
/build/k3s/agent/containerd/io.containerd.content.v1.content/blobs/sha256/<digest>:
blob not found
```

This is not the same as a missing GHCR tag. The registry manifest was resolved
and containerd began unpacking it, but local content metadata references a blob
that is absent from disk.

On `k3s1`, confirm that containerd knows the digest while the file is absent:

```bash
sudo /usr/local/bin/k3s ctr -n k8s.io content ls | grep <digest>

sudo ls -l \
  /build/k3s/agent/containerd/io.containerd.content.v1.content/blobs/sha256/<digest>
```

If the content record exists but the file does not, remove only that stale
content record:

```bash
sudo /usr/local/bin/k3s ctr -n k8s.io content rm sha256:<digest>
```

Then delete the failed pod so Kubernetes pulls the layer again:

```bash
kubectl -n home-budget delete pod <failed-pod>
```

Do **not** delete `/build/k3s`, the entire containerd directory, or unrelated
content records as a first response.

### Manual `crictl pull` and private GHCR images

`crictl` does not automatically use a Kubernetes `imagePullSecret`. A manual
pull can therefore fail with `401 Unauthorized` even though the pod can pull
through `ghcr-secret`:

```bash
sudo /usr/local/bin/k3s crictl pull ghcr.io/larocquemb/home-budget-pipeline:<tag>
```

A manual pull using incorrect credentials may instead return `403 Forbidden`.
Do not conclude that the pod's Kubernetes secret is broken from that result
alone. Inspect the pod's own pull event.

## 5. PostgreSQL is `0/1 Unknown` and the Service has no endpoint

Symptoms:

```text
postgres-0   0/1   Unknown
```

and:

```bash
kubectl -n home-budget get endpoints postgres
```

shows no endpoint. The schema hook then fails with:

```text
connection to server at <cluster-ip>, port 5432 failed: Connection refused
```

Check PostgreSQL first:

```bash
kubectl -n home-budget get pod postgres-0 -o wide
kubectl -n home-budget logs postgres-0 --tail=200
kubectl -n home-budget logs postgres-0 --previous --tail=200
kubectl -n home-budget get endpoints postgres
```

A healthy service has a pod endpoint such as `10.42.x.x:5432`.

For a normal recovery, recreate only the pod:

```bash
kubectl -n home-budget delete pod postgres-0
```

The StatefulSet reuses the existing PostgreSQL PVC.

## 6. Destructive PostgreSQL reset

Only use this procedure when the database is intentionally being discarded and
rebuilt from source receipts. It destroys the PostgreSQL database.

The PostgreSQL StatefulSet uses the `postgres-data-postgres-0` local-path PVC.
This is separate from `home-budget-data`, which contains receipt files on SMB.

Stop PostgreSQL:

```bash
kubectl -n home-budget scale statefulset postgres --replicas=0
```

Verify the PVC name:

```bash
kubectl -n home-budget get pvc
```

Delete only the PostgreSQL PVC:

```bash
kubectl -n home-budget delete pvc postgres-data-postgres-0
```

If its dynamically provisioned local-path PV remains in `Released`, delete that
PV as well. Do not delete `home-budget-data` as part of a database reset.

Recreate PostgreSQL:

```bash
kubectl -n home-budget scale statefulset postgres --replicas=1
kubectl -n home-budget get pod postgres-0 -w
kubectl -n home-budget get endpoints postgres
```

Proceed only after `postgres-0` is `1/1 Running` and the Service has a port
5432 endpoint.

## 7. `home-budget-data` PV stuck Terminating

`home-budget-data` is a static SMB PV using reclaim policy `Retain`. Its backing
share is `//pve/files` with subdirectory `brownrook/home-budget`. Kubernetes
object deletion must not be confused with deletion of the receipt files.

Inspect the PV:

```bash
kubectl get pv home-budget-data -o yaml | \
  grep -A12 -E 'deletionTimestamp:|finalizers:|claimRef:|persistentVolumeReclaimPolicy:'
```

A bound PV with a deletion timestamp and:

```text
finalizers:
- kubernetes.io/pv-protection
```

will remain `Terminating` while its PVC is still in use.

Find pods using the claim:

```bash
kubectl -n home-budget get pods -o json | \
jq -r '
.items[]
| select(any(.spec.volumes[]?; .persistentVolumeClaim.claimName=="home-budget-data"))
| [.metadata.name,.status.phase] | @tsv
'
```

Temporarily stop consumers:

```bash
kubectl -n home-budget patch cronjob receipt-processor \
  --type merge -p '{"spec":{"suspend":true}}'

kubectl -n home-budget scale deployment ledger-web --replicas=0
kubectl -n home-budget delete jobs -l app=receipt-processor
```

After no pods reference the PVC, a previously requested PVC deletion can
complete. Because the static PV uses `Retain`, the SMB files remain on the
backing share. Once the old PV/PVC Kubernetes objects are gone, Argo CD can
recreate them from Git.

Verify both are `Bound` before restoring workloads:

```bash
kubectl get pv home-budget-data
kubectl -n home-budget get pvc home-budget-data

kubectl -n home-budget scale deployment ledger-web --replicas=1
kubectl -n home-budget patch cronjob receipt-processor \
  --type merge -p '{"spec":{"suspend":false}}'
```

Avoid manually removing PV/PVC protection finalizers while workloads still
reference the volume.

## 8. Receipt processor CronJob is Degraded

Argo CD can be `Synced` but `Degraded` solely because the receipt processor is
unhealthy:

```text
CronJob home-budget/receipt-processor  Synced  Degraded
```

Inspect Jobs and run a clean manual test:

```bash
kubectl -n home-budget get jobs \
  -l app=receipt-processor \
  --sort-by=.metadata.creationTimestamp

kubectl -n home-budget delete jobs -l app=receipt-processor

kubectl -n home-budget create job \
  --from=cronjob/receipt-processor receipt-processor-manual

kubectl -n home-budget logs job/receipt-processor-manual
```

A healthy run should report `failed: 0`. `review_required` is not an
infrastructure failure; it means those receipts require data-quality review.

After a successful Job, verify Argo health:

```bash
argocd app get ledger --core | \
  grep -E 'Sync Status:|Health Status:'
```

Expected final state:

```text
Sync Status:   Synced
Health Status: Healthy
```

## 9. Recovery order

When several failures occur together, recover in dependency order:

1. Confirm the correct k3s kubeconfig/context.
2. Restore Argo CD repo-server so Git comparison works.
3. Repair image/containerd failures preventing hooks or workloads from starting.
4. Restore PostgreSQL and confirm the Service endpoint.
5. Complete the PreSync schema hook.
6. Verify the SMB receipt PV/PVC are `Bound`.
7. Restore `ledger-web` and the receipt CronJob.
8. Run one manual receipt-processing Job.
9. Require `failed: 0` and `Synced / Healthy` before declaring recovery complete.

This order avoids treating downstream symptoms as independent root causes.
