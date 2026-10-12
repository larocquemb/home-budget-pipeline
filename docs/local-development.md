# Local development on macOS

Develop and review receipts on the Mac, run Qwen workloads on the configured
model server, and use CI and K3s for integration and deployment validation.

## Services and ports

| Component | Local runtime | Address |
| --- | --- | --- |
| PostgreSQL 18 | Homebrew background service | `127.0.0.1:5432` |
| RabbitMQ | Homebrew background service | `127.0.0.1:5672` |
| RabbitMQ Management | Native RabbitMQ management plugin | `http://localhost:15672` |
| Caddy | Colima container; HTTPS entry point | `https://ledger-dev.brownrook.net` |
| OAuth2 Proxy | Colima container; Microsoft Entra sign-in | Behind Caddy |
| Ledger | Python process with automatic code reload | Port `8080`, behind the proxies |
| OCR worker and result collector | Per-user macOS LaunchAgents | Native RabbitMQ queues |

Caddy provides HTTPS. OAuth2 Proxy handles sign-in and forwards authenticated
requests to Ledger. Ledger and the collector use native PostgreSQL directly.
The local stack no longer runs a PostgreSQL container or uses
`.local-postgres/runtime.env`.

## One-time setup

Install the project into its virtual environment:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev,db,docs]'
```

Configure `.env.dev` from `.env.dev.example`. Set `DATABASE_URL` to your native
`home_budget` database on port 5432, `RABBITMQ_URL` to a local RabbitMQ user on
port 5672, and `RECEIPT_SOURCE_ROOT` to your receipt folder. Use `.ocr_cache` for
`HOME_BUDGET_OCR_CACHE`. URL-encode special characters in database and broker
passwords. Keep secrets in the ignored `.env.dev` file.

Start the installed Homebrew services and enable startup at login:

```bash
brew services start postgresql@18
brew services start rabbitmq
brew services start colima
```

Create `home_budget` if it does not exist:

```bash
createdb -h 127.0.0.1 -p 5432 home_budget
```

For a fresh database, initialize the application tables with
`make dev-db-reset`. This command deletes existing application schemas and data;
it is not part of the daily startup routine. It preserves source receipt files
and OCR caches.

For Entra sign-in, configure the values in `.env.dev`, add
`127.0.0.1 ledger-dev.brownrook.net` to `/etc/hosts`, and register
`https://ledger-dev.brownrook.net/ledger/oauth2/callback` as an Entra redirect URI.
Then start the proxies and trust Caddy's local certificate authority:

```bash
make dev-up
make dev-cert-install
```

Certificate installation requires the Mac administrator password. It is needed
only once while the Caddy certificate volume is retained. Restart the browser
after installation. See [network access](private-networking.md) for the proxy
and identity boundaries.

## Daily startup

Normally the infrastructure stays running. Check it with:

```bash
brew services list
colima status
pg_isready -h localhost -p 5432
rabbitmq-diagnostics ping
docker ps
```

PostgreSQL and RabbitMQ run independently of Colima. Caddy and OAuth2 Proxy use
`restart: unless-stopped`. Run `make dev-up` if those containers are stopped or
their configuration changes; it is not required for each editing session.

Start Ledger from the working tree:

```bash
make dev-web
```

Leave that terminal open. Source changes reload automatically. Open
`https://ledger-dev.brownrook.net/ledger/receipts`. A proxy message saying it cannot
connect to the upstream server usually means Ledger is not running; check:

```bash
curl http://localhost:8080/ledger/health
```

## Process receipts in the background

Enable the OCR worker and result collector once:

```bash
make dev-receipts-start
make dev-receipts-status
```

These LaunchAgents start at login, restart on exit, and read `.env.dev` whenever
they start. They do not require open terminals. Do not also launch duplicate
foreground workers or collectors unless you deliberately want extra consumers.

Queue receipts from one terminal:

```bash
source .venv/bin/activate
set -a
source .env.dev
set +a
ledger receipts process --verbose
```

The publisher exits after confirmed queue publication. The worker reads the
receipts and uses valid cached OCR when available; the collector saves results
to PostgreSQL. The current Mac OCR installation uses the CPU. Product enrichment
and remote Qwen inference are separate stages.

Choose the number of background OCR workers and threads per worker:

```sh
make dev-receipts-restart WORKERS=2 OCR_THREADS=4
make dev-receipts-status
```

This starts two independent RabbitMQ consumers and one collector. Each worker
handles one receipt at a time. `OCR_THREADS=4` sets PaddleOCR inference threads and BLAS thread limits to four.
The worker keeps `OMP_NUM_THREADS=1` for Paddle compatibility; each Tesseract
child gets `OMP_NUM_THREADS=4` and `OMP_THREAD_LIMIT=4`. Actual CPU use depends
on the OCR stage and cache hits. Worker count and thread settings are saved in
`.local-services/receipt-services.json`, so subsequent starts and telemetry
restarts retain them. Omitting settings on a fresh installation starts one
worker with the engines' default thread settings.

To reduce concurrency again:

```sh
make dev-receipts-restart WORKERS=1
```

Extra worker agents are stopped and removed. RabbitMQ redelivers unacknowledged
work if a worker is stopped during processing. `make test-receipts` publishes
test receipts; it does not configure worker concurrency.

Manage the background services with:

| Command | Effect |
| --- | --- |
| `make dev-receipts-status` | Show worker and collector process status. |
| `make dev-receipts-logs` | Follow all worker and collector log files; Ctrl+C stops only the viewer. |
| `make dev-receipts-restart` | Restart all worker and collector services, reloading `.env.dev` and code. |
| `make dev-receipts-stop` | Stop all worker and collector services and disable startup at login. |
| `make dev-receipts-start` | Enable and start worker and collector services; safe to repeat. |

The agents are installed under `~/Library/LaunchAgents/` with labels
`com.brownrook.home-budget.worker`, `com.brownrook.home-budget.worker-2`
(and subsequent workers), and `com.brownrook.home-budget.collector`.
Logs live in `.local-services/worker.log`, `.local-services/worker-2.log`
(and subsequent worker logs), and `.local-services/collector.log`. Alloy
discovers all `worker*.log` files, so the same Grafana query covers every worker.

## Send local logs and OpenTelemetry to Grafana

Install a native Alloy agent once:

```sh
brew install grafana-alloy
```

The agent sends directly across the LAN to
`monitoring.idc.brownrook.net:3100` (Loki logs) and `:4317` (OTLP traces and
metrics). Both connections require client certificates and verify the server
certificate. No SSH tunnel is used. Applications export to Alloy over loopback using OTLP HTTP/protobuf on
`127.0.0.1:4318`. This avoids gRPC fork interactions when OCR launches child
processes. Alloy forwards OTLP over verified mTLS to the monitoring server.
The monitoring Alloy routes traces to Tempo and metrics to Prometheus.

The Loki and OTLP monitoring firewall allowlists permit the trusted LAN
`192.168.2.0/24`. Client certificates remain required, and changing m4pro's
address within this subnet does not require a firewall update.
Apply the firewall policy from the repository root. These commands prompt for
the **monitoring host's sudo password**, and need no Grafana password:

```sh
ANSIBLE_CONFIG=ops/monitoring/ansible.cfg ansible-playbook \
  -i ops/monitoring/inventory/production.yml ops/monitoring/site.yml \
  --tags monitoring_firewall --ask-become-pass --check --diff
ANSIBLE_CONFIG=ops/monitoring/ansible.cfg ansible-playbook \
  -i ops/monitoring/inventory/production.yml ops/monitoring/site.yml \
  --tags monitoring_firewall --ask-become-pass
```

Set `MONITORING_PKI_DIR` in `.env.monitoring` to the local Brown Rook PKI
directory. The installer copies the CA and existing Loki/OTLP client
certificates and keys into the ignored, private `.local-services/telemetry/tls/`
directory. It validates the Alloy configuration, enables structured logs and
OpenTelemetry in `.env.dev`, and restarts the worker and collector:

```sh
make dev-telemetry-start
make dev-telemetry-status
make dev-telemetry-logs
```

Alloy runs as `com.brownrook.home-budget.alloy`, starts at login, and restarts
after failure. `make dev-telemetry-restart` reloads settings and copies renewed
certificates. `make dev-telemetry-stop` disables Alloy startup and turns off
application telemetry; the worker and collector continue processing.

In [Grafana Explore](https://grafana.idc.brownrook.net/explore), choose **loki**,
set the time range to **Last 1 hour**, and run:

```logql
{environment="development", host="m4pro", collection="local-file"}
```

Filter for a receipt filename or message ID:

```logql
{environment="development", host="m4pro", collection="local-file"} |= "20260214_sobeys_363_95.pdf"
```

This query includes OCR workers, the collector, enrichment, receipt publishing,
other Ledger CLI commands, the local web server, and Alloy itself. Enrichment
and CLI output remains visible in the terminal and is also captured to private
log files when `HOME_BUDGET_LOCAL_LOG_DIR` is configured by the telemetry
installer. Enrichment logs identify the receipt, selected items, per-item
progress, search/AI stages, completion, and failures. Restart `make dev-web`
once to enable capture of web access and error logs after updating the wrapper.
Production Kubernetes queries do not select local runs.

To graph fresh OCR receipts per minute, averaged over five minutes:

```logql
sum(
  count_over_time(
    {environment="development", host="m4pro", app="receipt-worker"}
    |~ `status=(review_)?results_published\b`
    |= "cache_hit=false"
    [5m]
  )
) / 5
```

Use query type **Range** for a graph or **Instant** for the current rate.
The completion log records the parser's actual `cache_hit` outcome, including
cache reuse during retries. A receipt is counted only after its OCR results
have been published successfully; this is not the collector's database commit.
Review-required receipts are included. Cached completions, cache-only rebuilds,
and older logs without this field are excluded. Restart receipt services once
after updating the code to emit the flag; existing log history is unchanged.

To see enrichment only:

```logql
{environment="development", host="m4pro", app="product-enrichment"}
```

Command logs cover future runs after sourcing the updated `.env.dev`; output
from commands run before forwarding was enabled is not recovered. OpenTelemetry resources use `deployment.environment.name=development`
and the Mac's host name; service names distinguish the worker and collector.
See [receipt OpenTelemetry](opentelemetry.md) for the backend dashboards.

Check `make dev-telemetry-logs` for export failures when Grafana is empty.
An agent being running does not prove that the firewall permits delivery.

## Find receipts and enrich products

On **Receipts**, use **Search receipts** to search all pages by merchant, date,
total, filename, expense ID, or item description. For example,
`Sobeys 2026-02-14` or `363.95` can find a purchase without knowing its expense
number. Click any column heading to toggle ascending and descending order.
Search and sorting remain applied across pages.

Refresh to update the **Processing** column. The UI does not yet stream logs or
automatically refresh progress. See the [user guide](user-guide.md) for status
meanings and OCR provenance.

**Not enriched** and **Awaiting product identification** concern product
descriptions, not OCR progress. After importing a receipt, run enrichment for
its local expense ID with a configured `BRAVE_SEARCH_API_KEY`:

```bash
ledger receipts enrich --receipt EXPENSE_ID --write-db
```

Replace `EXPENSE_ID` with the number shown in the local UI. This is the automatic
enrichment path; the separate collaborative workflow saves recommendations for
human review. A Brave HTTP 402 with `USAGE_LIMIT_EXCEEDED` requires resolving the
API plan allowance or loading a key for an available plan before retrying.

## Build, test, and merge

Run affected unit tests while editing, then use `make check-local` before
publishing a completed change. It runs the unit suite, builds a wheel, and builds
the documentation with strict validation.

Integration tests use the disposable `home_budget_test` database. Use a separate
test broker, such as `compose.queue-test.yaml` on port 5673, and set
`TEST_RABBITMQ_URL` explicitly. Do not alias it to the development `RABBITMQ_URL`.
Tests can leave synthetic receipts such as `OCR-GARBAGE` in their test database;
the Ledger UI should use `home_budget` instead.

Open a pull request when the change is ready. CI runs integration checks; merging
to `main` triggers the image build and GitOps image update. See the
[operations runbook](operations-runbook.md) for test and deployment commands.

To enrich only the first five items on the Sobeys test receipt:

```sh
ledger receipts enrich \
  --receipt '2026-02-14/20260214_sobeys_363_95.pdf' \
  --limit 5 --write-db
```

`--limit` selects the first N items ordered by database item ID (default: 100).
Already accepted items in that selection may be skipped; later receipt items
are not substituted. This limits product enrichment, while OCR still processes
the whole receipt. Omit `--write-db` to preview enrichment results.

## See receipts processing in parallel

The receipts/min chart counts completion events. Use the **Receipt processing
 overlap by worker** state timeline in **Home Budget Receipt Telemetry** to
see actual processing activity instead. Each worker/receipt has its own row;
coloured intervals that overlap indicate parallel processing. Workers emit
activity at start, every 15 seconds while processing, and at completion or
handled failure. The signal expires after 30 seconds if a worker stops abruptly.
The visualization is sampled, so short cached jobs can be missed and interval
edges can differ from exact start/end timestamps. Restart receipt services to
load the signals. Apply the monitoring playbook to update the provisioned panel:

```bash
make dev-receipts-restart
ANSIBLE_CONFIG=ops/monitoring/ansible.cfg ansible-playbook \
  -i ops/monitoring/inventory/production.yml ops/monitoring/site.yml \
  --ask-become-pass
```

For exact recorded attempt times, PostgreSQL already retains independent start
and terminal events in `lineage.receipt_events`. No worker restart is needed
for this report:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f sql/reports/receipt_processing_timeline.sql
```

If this tries port `5433`, your shell still has another database URL loaded.
Run the report against native development PostgreSQL explicitly:

```bash
psql 'postgresql://paul@127.0.0.1:5432/home_budget' -v ON_ERROR_STOP=1 \
  -f sql/reports/receipt_processing_timeline.sql
```

The report covers attempts started within the past 24 hours and lists receipt
filename, worker host/PID, start, finish, elapsed minutes, status, and attempt ID.
Retries remain separate attempts even for the same filename. Overlapping
start/finish intervals on different workers show concurrency. A null finish
means no terminal event was recorded, not proof that the worker is still alive.
Heartbeats are also retained in PostgreSQL under the same attempt ID as the
start/end events and live logs. The report adds `last_seen_at`, `live_state`,
and `cache_hit`: an unfinished attempt with activity in the last 45 seconds is
`processing`; older activity is `heartbeat_stale`. Heartbeats do not replace the
recorded outcome. Cache use is nullable for older completion events. Restart
receipt services to enable heartbeat persistence; existing recorded intervals
remain available without a restart.
Elapsed time for unfinished attempts is measured to the report time. The
terminal event measures worker result publication or failure, not the collector's
later database commit. Missing lineage events cannot be reconstructed by this
report. No PostgreSQL datasource is added to Grafana by this change; the
provisioned activity timeline uses Loki.

### Prepare PostgreSQL TLS for a Grafana connection

Grafana is suitable for operational reports such as worker overlap, throughput,
and failures. The Ledger UI should expose individual receipt status and history
with links to the associated expenses. The recorded processing attempts can
support both views. A Grafana PostgreSQL connection needs a dedicated read-only
role and a verified TLS server certificate before enabling LAN access.

Prepare a separate server key and CSR on m4pro, using its LAN hostname:

```bash
make dev-postgres-tls-prepare POSTGRES_HOST=m4pro.local
```

The helper creates an ignored, private `.local-services/postgres/tls/` directory
and refuses to overwrite an existing key or request. Keep `server.key` on m4pro.
It prepares `DNS:m4pro.local` in the CSR and a GnuTLS signing template; it does
not sign a certificate or change PostgreSQL. The hostname is independent of
the Mac's DHCP address. Monitoring must resolve it through mDNS; see the
"Resolve Mac hostnames on the LAN" section in `ops/monitoring/README.md`.
Grafana's datasource must use `m4pro.local:5432` and pass its connection health
check. A successful `getent` lookup alone does not verify Grafana's resolver.

The default `POSTGRES_HOST` is `m4pro.local`. An explicit IPv4 address is also
supported and produces an IP SAN instead; the older `POSTGRES_LAN_IP` option
continues to work. An IP certificate requires a stable address. If you already
prepared an IP-only CSR, the helper will refuse to overwrite it. Prepare the
hostname identity in a separate directory:

```bash
bash scripts/prepare_receipt_postgres_tls.sh m4pro.local \
  .local-services/postgres/tls-hostname
```

Use that directory for the signing and verification steps below when applicable.

Sign `server.csr` using the Brown Rook Intermediate CA ceremony described in
[the certificate instructions](opentelemetry.md#issue-or-rotate-telemetry-certificates),
substituting the prepared `server.tmpl` and `server.csr` for the telemetry leaf
inputs. Save the issued leaf as `server.crt` in the same directory, then build
the server chain:

```bash
cat .local-services/postgres/tls/server.crt \
  "$HOME/brownrook-ca/intermediate/intermediate_ca.crt" \
  > .local-services/postgres/tls/server.fullchain.crt
openssl verify -purpose sslserver -verify_hostname m4pro.local \
  -CAfile "$HOME/brownrook-ca/root/root_ca.crt" \
  -untrusted "$HOME/brownrook-ca/intermediate/intermediate_ca.crt" \
  .local-services/postgres/tls/server.crt
```

These are preparation steps. PostgreSQL LAN access and the Grafana PostgreSQL
datasource still need to be configured after the signed certificate is ready.

When the signed identity is in `~/brownrook-ca/leafs/m4pro-postgres/`, with
`m4pro-postgres.crt`, `m4pro-postgres.fullchain.crt`, and its matching `server.key`,
check and configure the native PostgreSQL service:

```bash
make dev-postgres-grafana-check
make dev-postgres-grafana-apply
brew services restart postgresql@18
```

The restart briefly disconnects existing database clients, so choose an
appropriate time during receipt processing. The check command reads `.env.dev`
directly and requires the local `home_budget` database on port 5432. It verifies
the CA chain, server purpose, hostname, lifetime, matching key, and full chain.
Apply creates `grafana_receipt_reader` with SELECT access to a projected
`lineage.receipt_processing_attempts` view covering the last 24 hours. It rejects
existing elevated reader privileges and checks that other application tables
are inaccessible. A private generated password is retained in
`.local-services/postgres/grafana-reader.password`; it is never printed.

Apply enables TLS and the IPv4 listener. The managed HBA block allows the reader
only from monitoring (`192.168.2.202/32`) with TLS and SCRAM authentication, and
rejects that account from other network addresses. Existing native authentication
rules are preserved. The helper does not restart PostgreSQL or receipt workers.

The Grafana datasource remains a separate configuration step: use
`m4pro.local:5432`, database `home_budget`, user `grafana_receipt_reader`, the
private reader password, and TLS `verify-full` with the Brown Rook root CA.
Query the projected view rather than raw receipt or expense tables. Test the
connection from Grafana after the PostgreSQL restart.
