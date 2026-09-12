# BrownRook Ledger documentation

BrownRook Ledger turns receipt evidence into canonical household expenses,
categorized line items, review data, and budget reports.

The application and its primary command-line program share the Ledger name.
Run `ledger --help` for available operational commands. Existing automation can
continue using the compatible `brownrook` alias.

## Use Ledger

Start with the [user guide](user-guide.md) to review receipts, extraction
quality, canonical expenses, category decisions, duplicates, and transaction
matches.

## Understand the system

The [solution architecture](solution-architecture.md) explains the runtime
components, receipt-processing flow, persistence boundaries, and GitOps delivery
path.

## Operate Ledger

- Use the [operations runbook](operations-runbook.md) for local development,
  tests, deployment, and rollout verification.
- Use the [troubleshooting guide](troubleshooting.md) for Kubernetes, Argo CD,
  PostgreSQL, storage, and receipt-processing recovery.
- Use the [network access guide](private-networking.md) for local, public,
  private-LAN, DNS, TLS, and service exposure boundaries.
- Use the [RabbitMQ runbook](rabbitmq-receipts.md) when queued receipt processing
  is enabled.
- Use the [Kubernetes authentication guide](kubernetes-auth.md) for Entra ID,
  OAuth2 Proxy, ingress, and secret setup.

The project source and contribution history are available in the
[GitHub repository](https://github.com/larocquemb/home-budget-pipeline).
