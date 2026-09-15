# Public live telemetry demo

KAN-119 publishes a deliberately small, aggregate-only Grafana dashboard for
the Brown Rook public website. The externally shared dashboard is live and does
not require a Grafana account. Every ordinary Grafana dashboard, API, Explore,
log, and trace route remains authenticated.

All three Git-managed telemetry dashboards use Grafana's stable
`dashboard.grafana.app/v2` resource model. Ansible reconciles them through the
native dashboard API; it does not install classic dashboard JSON through a file
provider. It likewise reconciles the `Home Budget` and `Brown Rook` folders
through `folder.grafana.app/v1`, adopting a pre-existing folder with the same
title during migration so duplicate folders are not created. The private
receipt and OCR dashboards live in `Home Budget`; the sanitized public demo
lives in `Brown Rook`.

The one-time conversion removes the classic provider while Grafana is stopped
and recreates each dashboard with the same UID. Grafana dashboard version
history is reset by that conversion. The public demo access token is pinned to
the already-published value, so the brownrook.com link remains stable even if
Grafana has to recreate the external-share database row.

The public URL is a bearer capability: anyone who has it can see the dashboard
until sharing is disabled. It contains no password and must never be reused for
an operational dashboard. The public hostname is
`telemetry.idc.brownrook.com`; the private Grafana hostname remains available
only on the LAN/VPN.

## Public request path

The public site links directly to `telemetry.idc.brownrook.com`. Public DNS
aliases that name to the dynamic `idc.brownrook.com` site anchor. The existing
edge proxy retains the Host header and forwards HTTPS traffic to Traefik. The
`deploy/public-telemetry` resources then route only these Grafana paths to the
monitoring LXC at `192.168.2.202:3000`:

- `/public-dashboards`;
- `/bootdata`;
- `/public`; and
- `/api/public/dashboards`.

There is deliberately no `/` catch-all. Login, Explore, ordinary dashboard,
and ordinary Grafana API paths therefore receive a Traefik 404 on the public
hostname. Grafana global anonymous access also remains disabled as a second
boundary. The ingress applies response-security headers and a per-client rate
limit sized for the five-session acceptance test. On the monitoring LXC,
nftables accepts direct Grafana port 3000 connections only from loopback, the
edge proxy, and the K3s node; other LAN clients cannot bypass the approved
proxy paths. Grafana's single-domain redirect is disabled because this instance
serves both the private and public hostnames; the proxy allowlist and
host-and-path-specific routes provide the DNS-rebinding boundary instead.

The three hosts have distinct roles:

| Address | Role |
| --- | --- |
| `192.168.2.202` | Monitoring LXC and Grafana backend |
| `192.168.2.210` | Edge Nginx proxy and private Grafana DNS target |
| `192.168.2.230` | K3s node and Traefik public-path proxy |

## Published data boundary

The public dashboard includes only:

- receipt attempts completed during the fixed six-hour window;
- aggregate successful, review-required, and failed ratios;
- aggregate receipt throughput and p50/p95 processing latency; and
- aggregate OCR pass throughput and p50/p95 latency.

It deliberately has no dashboard variables, annotations, exemplars, drilldown
links, logs, or traces. Its queries do not group by or display infrastructure
hostnames, IP addresses, application service names, queue names, receipt
identifiers, file paths, hashes, run/pass identifiers, or financial data. The
time picker is hidden and external time selection is disabled. Grafana refreshes
the fixed six-hour view every 60 seconds.

Grafana executes externally shared dashboard queries on the backend rather than
accepting arbitrary viewer-supplied queries. This repository still treats the
dashboard JSON itself as the primary disclosure boundary and tests it for the
forbidden fields above.

## Reconcile and obtain the public URL

From `home-budget-pipeline`, validate and review the Git-managed state before
applying it:

```sh
make test-observability
make monitoring-gitops-syntax
make monitoring-gitops-check
make monitoring-gitops-apply
```

First reconcile the public DNS CNAME from `brownrook-edge`, then sync the
`ledger` Argo CD Application so its certificate, restricted ingress, service,
and endpoint are active. The monitoring apply prints a line beginning with
`Public live demo:`. Copy that exact URL
into the `Live Services` section of `brownrook-web/site/index.html`. The website
should link to the dashboard in a new tab and label it as a live, public demo;
do not embed Grafana or enable Grafana's global anonymous mode.

The role verifies all of the following during apply:

- Grafana reports `auth.anonymous.enabled=false`;
- Grafana reports `public_dashboards.enabled=true`;
- the two folders and all three dashboards match their native v1/v2 resources;
- the separate sanitized v2 dashboard has no variables;
- its externally shared state has annotations and time selection disabled;
- its public route returns HTTP 200 without credentials; and
- `/`, login, Explore, health, and ordinary Grafana APIs return HTTP 404 on the
  public hostname because Traefik does not route them.

## Acceptance checks

Before browser testing, prove the hostname exists in public DNS and does not
resolve to RFC1918 space:

```sh
dig @1.1.1.1 +short telemetry.idc.brownrook.com
```

Open the public URL in a private browser window that has never authenticated to
Grafana. Confirm that it opens without a login prompt, updates after 60 seconds,
and shows only the eight aggregate panels. Repeat from a phone on cellular data.
The cellular check is mandatory: a successful LAN request does not prove public
reachability.

In the browser developer tools, inspect the document and XHR/fetch responses.
Search for `192.168.`, `.idc.`, `worker_host`, `instance`, `queue`, `vhost`,
`trace_id`, `run_uuid`, `pass_id`, `receipt_hash`, and `source_reference`; none
may reveal a value. Direct navigation to the normal Grafana dashboard and
Explore routes must still require authentication.

For a short capacity check, leave one private browser session refreshing for 15
minutes, then repeat with five parallel sessions. During each run, verify on the
monitoring host that Grafana and Prometheus stay active and that memory, load,
and filesystem use remain within the monitoring LXC's existing headroom:

```sh
systemctl is-active grafana-server prometheus
free -h
uptime
df -h /
journalctl -u grafana-server -u prometheus --since '-20 minutes' \
  --no-pager -p warning
```

Record the private-browser, mobile, disclosure, and capacity results in KAN-119
before closing the story.

## Disable or revoke

Remove the link from `brownrook-web` first so the high-availability public site
does not retain a dead link. Then set:

```yaml
monitoring_grafana_public_demo_enabled: false
```

and run the normal monitoring check and apply. GitOps patches the existing
external share to disabled, making its access token unusable while leaving the
private Git-managed dashboard available for review. Restore the value to
`true` and apply to re-enable the same link.

If the URL itself must be rotated, delete the external share in Grafana after
removing the website link, then apply the enabled Git state to create a new
access token. Publish the newly printed URL through a reviewed `brownrook-web`
change.
