# RabbitMQ receipt processing runbook

This page covers setup, commands, recovery, and deployment. See
[RabbitMQ receipt processing design](rabbitmq-receipt-design.md) for the message
lifecycle, reliability boundaries, idempotency model, and failure behavior.

The queued processing mode reuses backlog discovery, OCR, evidence persistence,
and `ingest.receipt_processing_status`. The existing local commands and default
`k8s` deployment still work. No database migration, Celery service, or new work
table is required.

## Contract and delivery

One UTF-8 JSON message describes one immutable source file:

```json
{"version":1,"source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_reference":"2026/receipt.pdf","attempt":1}
```

`source_reference` is a POSIX path relative to `RECEIPT_SOURCE_ROOT`. Publishers
and workers need the same files beneath that root, though their absolute mount
paths can differ. Absolute paths, parent traversal, escaping symlinks, malformed
hashes, unsupported versions, and malformed messages are rejected. Workers check
the actual hash before and after parsing. Put complete files in the inbox using
an atomic rename; do not edit a file while it is being processed. The AMQP
`message_id` is `receipt.v1:<source_sha256>` and remains stable across retries.

The publisher uses persistent messages, mandatory routing, and publisher
confirmations. A failed or ambiguous publication makes the command fail; a later
scan republishes unfinished sources. Discovery collapses duplicate hashes within
one scan. Rediscovery and the commit/ACK failure window can produce duplicates.
This is at-least-once delivery, with idempotency for the same SHA-256.

Each worker holds a PostgreSQL session advisory lock for the source hash, then
checks completion again. Evidence, expense writes, and the final `succeeded` or
`review_required` status commit in one transaction before ACK. A redelivery after
that commit skips OCR and persistence. Different hashes can run concurrently.
Local backlog processing uses the same per-source lock. A worker crash releases
the database lock when its session closes; RabbitMQ requeues unacknowledged work.
Use direct PostgreSQL connections or session pooling, not transaction pooling.

Each worker handles one receipt at a time (`prefetch=1`). OCR/DB work runs on a
worker thread while the connection thread services heartbeats. SIGTERM stops new
work and lets the current receipt finish. If Kubernetes kills it after the grace
period, the unacknowledged delivery returns to the queue. Broker disconnection
causes process exit, and the Deployment restarts the worker.

## Retry and dead letters

The application declares three durable quorum queues, each bound to a direct
exchange of the same name with routing key `receipt`:

| Queue | Purpose |
| --- | --- |
| `receipts.v1.work` | Receipt processing |
| `receipts.v1.retry` | Wait 60 seconds, then dead-letter back to work |
| `receipts.v1.dead` | Invalid messages and exhausted work for inspection |

Failures publish a new message to retry with an incremented attempt, then ACK the
original only after confirmation. The third failed delivery goes to the DLQ.
Malformed messages and changed source contents go there immediately. A failed
retry/DLQ publication leaves the original unacknowledged. Broker-side transfers
use quorum at-least-once dead lettering and `reject-publish` overflow, so the
source retains a message until the destination accepts it. See the
[RabbitMQ quorum queue documentation](https://www.rabbitmq.com/docs/quorum-queues).

The existing database attempt counter also limits each source to three processing
starts, including interrupted starts. Scheduled discovery skips exhausted hashes;
fresh publication cannot silently reset that counter. Connection failures before
a database attempt have only the message's retry budget. The work queue's delivery
limit of 20 additionally bounds repeated crashes without application settlement.
Completed receipts requiring human review count as completed and are ACKed.

The DLQ keeps the original body, stable message ID when valid, and an `error_type`
header. Worker logs include the hash, status or routing destination, and error
type. Detailed processing errors remain in the existing database status table.
Monitor work depth, unacknowledged work, retry depth, DLQ depth, and worker restarts.
All queues reject new publications at 100,000 messages rather than dropping old
work. Queue arguments are deployment constants; changing them requires draining
and recreating the affected queues or introducing a new versioned prefix.

To recover an exhausted receipt, pause publication and workers and inspect the
DLQ and `ingest.receipt_processing_status`. Fix the source/configuration, remove
stale deliveries for that hash, then use the CLI to reset its attempt budget:

```sh
ledger receipts retry '2026-08-14/receipts_20260814_0001.pdf'
ledger receipts publish
ledger receipts consume
```

Use the exact path relative to the receipt source root. `retry` resets only the
matching failed or interrupted receipt's processing status and attempt budget;
it preserves source identity and canonical evidence. It refuses active processing
locks, completed receipts (including review-required receipts), and ambiguous
source references. A successful command reports `retry_ready: true`. It requires
only database access and prepares the receipt for the next `publish` or local
`process` command; it does not publish or remove broker messages itself. Do not
simply move an attempt-3 DLQ body back to work.
For intentional OCR refresh, drain/stop workers and use the existing local
`receipts process --refresh-ocr-cache` command. Stop all writers before rebuilding
or switching the ingest schema; its completion state is the idempotency record.

## Run and validate

Install the project with its database dependencies
(`python -m pip install -e '.[db,dev]'`).
Set `DATABASE_URL`, `RABBITMQ_URL`, and optionally `RECEIPT_SOURCE_ROOT` and
`HOME_BUDGET_OCR_CACHE`. If these are stored in `.env.dev`, export the assignments
so Python can read them (plain `source` does not export new shell variables):

```sh
set -a
source .env.dev
set +a
```

Broker credentials must have configure/write/read access
to the queues and exchanges in a dedicated vhost. Then run:

```sh
ledger receipts publish /shared/receipts
ledger receipts consume /shared/receipts
```

The Kubernetes publisher and worker use the same `ledger receipts` commands.
Add consumers to increase concurrency.

```sh
# Disposable local RabbitMQ; use only the separate test database.
docker compose -f compose.queue-test.yaml up -d --wait
TEST_RABBITMQ_URL='amqp://test:test@localhost:5673/%2F' \
  make test-db
docker compose -f compose.queue-test.yaml down -v
```

The disposable broker's management UI is available at
`http://localhost:15673` with the test username and password from
`compose.queue-test.yaml`. For the Kubernetes overlay, use:

```sh
kubectl -n home-budget port-forward svc/rabbitmq 15672:15672
```

Then open `http://localhost:15672` and sign in with the credentials stored in
`rabbitmq-secret`.

The repository's test hook bootstraps the local `home_budget_test` database.
When overriding `TEST_DATABASE_URL`, provision the disposable database and load
the schema as in CI. PostgreSQL tests launch separate worker processes to verify
same-source serialization, different-source concurrency, rollback, attempt
limits, lock release, and commit-before-ACK replay. Broker tests cover confirmed
publishing, unroutable messages, unacknowledged redelivery, delayed retries, DLQ
routing, and consumer shutdown. CI supplies both services. Unit tests need no
external services: `make test`.

## Kubernetes rollout

`deploy/rabbitmq` is an opt-in Kustomize overlay over the existing `k8s` resources.
It changes the existing receipt CronJob to discovery/publication, adds one OCR
worker Deployment, and adds a single RabbitMQ StatefulSet with an 8 GiB
`local-path` volume. The publisher and worker use the existing shared
`home-budget-data` PVC. The worker image follows the immutable image selected by
CI for the publisher. Use an application image built from this change.

Create `rabbitmq-secret` from `k8s/rabbitmq-secret.example.yaml` using real
credentials; keep the password and URL-encoded password in `RABBITMQ_URL`
consistent. The broker initializes the `receipts` vhost. Secrets are deliberately
excluded from the overlay. Review the rendered configuration:

```sh
kubectl kustomize deploy/rabbitmq
```

During rollout, suspend the existing receipt CronJob, let its active local job
finish, then switch the deployment target from `k8s` to `deploy/rabbitmq`. Confirm
broker readiness and worker startup before allowing the publisher CronJob to
resume. Applying this overlay normally is `kubectl apply -k deploy/rabbitmq`;
in GitOps, change the application's path instead. Scale `receipt-worker` for
additional workers. No cluster resources are changed by rendering the overlay.

This broker is persistent but single-node, not highly available. Local storage
loss can lose queued work; inbox rediscovery recovers unfinished sources within
their attempt budget. A replicated broker is a separate operational slice.
The management service is internal; no public ingress is included. The worker
has a 15-minute shutdown grace period, and the broker ACK timeout is one hour.

For authenticated access to the management UI from the corporate LAN, use the
`deploy/rabbitmq-private` overlay. It exposes only port 15672 through the private
hostname at `rabbitmq.brownrook.net`; AMQP remains cluster-internal.
RabbitMQ's own user and password are still required. The required private DNS,
TLS secret, Traefik entry point, and firewall controls are documented in the
[network access guide](private-networking.md).

To roll back, suspend publishing, drain work/retry queues, and stop workers before
restoring the `k8s` deployment target. Preserve broker storage and DLQ contents
until outstanding failures have been resolved. Plain `kubectl apply` of the base
does not delete the worker/broker resources added by the overlay; scale workers
down explicitly or use the existing GitOps pruning workflow.
