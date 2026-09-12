# BrownRook Ledger solution architecture

BrownRook Ledger turns receipt files and electronic purchase records into
canonical expenses, categorized line items, review data, and budget reports.
PostgreSQL is the system of record. Receipt files and OCR cache artifacts stay
on shared storage; RabbitMQ carries work references only.

## Runtime components and data flow

```mermaid
flowchart LR
    user[Ledger user]
    entra[Microsoft Entra ID]
    proxy[NGINX and Traefik]

    subgraph cluster[K3s home-budget namespace]
        oauth[OAuth2 Proxy]
        web[Ledger web application]
        cron[Receipt processor CronJob]
        publisher[Receipt publisher]
        rabbit[(RabbitMQ work, retry, and dead queues)]
        worker[Receipt worker]
        enrich[Product enrichment job]
        db[(PostgreSQL)]
        files[(Shared receipt files and OCR cache)]
    end

    sources[Scans, PDFs, and electronic receipt sources] --> files

    user -->|HTTPS /ledger| proxy --> oauth
    oauth <-->|sign in| entra
    oauth -->|authenticated identity| web
    web -->|read reports and audited corrections| db
    web -->|receipt preview| files

    files --> cron
    cron -->|base deployment: discover, OCR, normalize| db

    files --> publisher
    publisher -->|optional queued deployment: confirmed v1 message| rabbit
    rabbit -->|manual-ACK delivery| worker
    worker -->|read and verify source| files
    worker -->|OCR, normalize, reconcile, persist| db
    worker -->|transient retry or terminal failure| rabbit

    enrich -->|read items and save accepted evidence| db
    enrich -->|product search; optional query expansion| providers[Brave Search and optional OpenAI]
```

The base Kubernetes deployment runs receipt discovery and processing in one
CronJob every 15 minutes. The optional `deploy/rabbitmq` overlay changes that
CronJob into a publisher and adds RabbitMQ plus a long-running worker. Both
modes call the same processing code and use the same PostgreSQL status tables.

The main processing path is:

1. Discover a source file and calculate its SHA-256 hash.
2. Check PostgreSQL processing and evidence state for completion or exhaustion.
3. Run OCR, parse and normalize the receipt, and reconcile totals.
4. Store OCR evidence, the canonical expense, line items, and final processing
   status in PostgreSQL.
5. Apply deterministic and approved learned category rules, with optional AI
   classification only after rule-based matching fails.
6. Present receipts, expenses, review queues, reconciliation, duplicates, and
   category reports in Ledger.

In queued mode, delivery is at least once. A PostgreSQL advisory lock serializes
workers for the same source hash, and completed database state makes a duplicate
delivery safe. The consumer acknowledges a message only after the database
result or a retry/dead-letter transfer is durable. See the
[RabbitMQ design](rabbitmq-receipt-design.md) for those boundaries in detail.

## Persistence boundaries

| Component | Stores | Role |
| --- | --- | --- |
| Shared storage | Original receipt files and OCR cache JSON | Durable source evidence and reusable extraction artifacts |
| PostgreSQL | Processing state, OCR runs, evidence, canonical expenses, line items, categories, audit history, and analytics views | Authoritative application state |
| RabbitMQ | Versioned source hash and relative-path work messages | Optional transient work distribution, retry delay, and dead-letter inspection |

Ledger read queries use read-only database transactions. Category overrides,
category rules, and item description/product-link corrections use explicit
transactions and record the authenticated user in audit history.

## Build and deployment flow

```mermaid
flowchart LR
    change[Pull request] --> checks[GitHub Actions application tests]
    change --> doccheck[Strict MkDocs documentation build]
    checks --> merge[Merge to main]
    doccheck --> merge
    merge --> image[Build amd64 application image]
    image --> registry[(GitHub Container Registry)]
    image --> update[Commit immutable image tag to k8s/kustomization.yaml]
    update --> argo[Argo CD automated sync]
    argo --> migrate[PreSync database setup job]
    migrate --> workloads[PostgreSQL, Ledger web, receipt jobs, and optional queue worker]
    merge --> docs[Build versioned Markdown with MkDocs Material]
    docs --> pages[GitHub Pages online manual]
```

The PostgreSQL init container uses the stable schema-bootstrap image for a new
volume. Existing databases are upgraded by the versioned PreSync job before
application workloads roll forward. Kubernetes secrets and storage credentials
are created outside Git.

For operator commands and recovery procedures, see the
[operations runbook](operations-runbook.md). For end-user review workflows, see
the [Ledger user guide](user-guide.md).
