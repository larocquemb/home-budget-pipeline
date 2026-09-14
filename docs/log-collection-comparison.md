# Kubernetes log collection comparison

This page records measured KAN-86 results for the in-cluster Fluent Bit and
external Grafana Alloy collection paths. It is deliberately separate from the
[design](log-forwarding-design.md) and the
[operations runbook](log-forwarding.md): designs state intended behavior,
while this page records what was observed.

## Status

The production topology, mandatory Loki mTLS, Grafana access, Pod replacement,
Loki outage, one collector-restart trial, and a live receipt-worker event were
verified on 2026-09-14. KAN-86 was accepted as complete after both collection
methods returned the same live application event through Grafana. The broader
benchmark tests listed below were deliberately deferred and are not required
for this functional-verification scope.

## Test environment

| Item | Value |
| --- | --- |
| Cluster | `brownrook-k3s1` |
| Namespace | `home-budget` |
| K3s worker nodes | One measured node, `k3s1` |
| Agent path | Fluent Bit `4.2.0` DaemonSet |
| API path | Grafana Alloy `1.19.2` systemd service |
| Backend | Single-process filesystem Loki on the monitoring host |
| Query path | Grafana datasource proxy using datasource UID `cfy4qg8i178jkb` |
| Security state | Loki `RequireAndVerifyClientCert` plus nftables source allowlist |

Synthetic tests emitted a monotonically increasing `sequence=N` once per
second. After stopping the source and allowing delivery to settle, the same
Grafana query was grouped by `collection`. Missing values, duplicates, and
path-only values were calculated from the unique integer sequences.

Run the committed analyzer for another sequenced marker:

```sh
scripts/compare_log_collection_probe.sh MARKER 1h
```

It prompts for the Grafana administrator password through `curl`; the password
is not accepted as a command-line argument or stored in Git.

## Results summary

| Test | Fluent Bit | Kubernetes API and Alloy | Observation |
| --- | --- | --- | --- |
| Initial dual delivery | Probe present | Probe present | Grafana returned both `collection` values |
| Live receipt-worker event | Exact terminal event present | Exact terminal event present | Both paths returned the same receipt hash and `status=review_required` through Grafana |
| Application Pod replacement | 46 old-Pod and 50 replacement-Pod records | 46 old-Pod and 50 replacement-Pod records | Exact counts for both Pod identities |
| Loki outage and recovery | 269/269 unique, no gaps or duplicates | 269/269 unique, no gaps or duplicates | Exact sequence sets matched after recovery |
| Collector restart | 166/167 unique, sequence 114 missing, no duplicates | 167/167 unique, no gaps or duplicates | One Fluent Bit record gap in this trial |

These are small controlled samples, not service-level guarantees. The
collector-restart result must be repeated before treating its one-record
difference as representative.

## Resource and latency baseline

The following are single-point observations taken after both collectors were
active. They are not normalized benchmark results.

| Measurement | Fluent Bit | Alloy |
| --- | --- | --- |
| Placement | One Pod on `k3s1` | One process on monitoring host |
| CPU observation | `3m` from Kubernetes metrics | `0.4%` from `ps` |
| Resident memory | `32 MiB` | `65,856 KiB` from `ps`; exporter later reported about `73.3 MiB` |
| Delivered volume at sample | 1,521 records / 497,152 bytes | 118,580 records / 8,960,756 bytes |
| Dropped records | 0 | 0 for every reported reason |
| Historical retries before outage test | 0 | 24 batches, corresponding to 24 HTTP 500 responses |
| Mean reported propagation/output latency | About 0.500 seconds | About 0.391 seconds |
| Records within one second | 1,382/1,384, about 99.9% | 110,699/118,580, about 93.4% |

The counters cover different process lifetimes and workloads. Fluent Bit cost
is per node and scales with cluster size; Alloy is centralized and its process
also performs discovery, relabeling, processing, and WAL management. The CPU
units come from different tools and should not be compared as if they were the
same sample. A synchronized sustained-load test is still required.

## Pod replacement test

A controlled Deployment emitted `KAN86_RESTART_PROBE` every two seconds. Its
original Pod was deleted while logging, and Kubernetes created a replacement.
Grafana returned:

| Collection | Deleted Pod | Replacement Pod |
| --- | ---: | ---: |
| `fluent-bit` | 46 | 50 |
| `kubernetes-api` | 46 | 50 |

Both collectors retained the observed tail of the deleted Pod and began
collecting the replacement. This test did not restart either collector.

## Loki outage test

A second Deployment emitted `KAN86_LOKI_OUTAGE_PROBE` every second. Loki was
stopped while the source continued from sequence 32 through at least sequence
85. Loki was then started and returned HTTP 200 from `/ready` after its ring
stabilized.

The final Grafana result was identical for both paths:

- sequences 0 through 268;
- 269 unique records;
- zero missing sequences;
- zero duplicates; and
- zero records present in only one collection.

Fluent Bit retries rose from zero to 134 during the outage and to 221 after
recovery. Its filesystem chunks rose from one to 64, then drained back to one.
Output errors, abandoned retries, and dropped records remained zero.

Alloy batch retries rose from 24 to 30. Sent entries rose from 118,580 to
120,006, all dropped-entry counters remained zero, and its WAL watcher remained
active. Application-side `kubectl logs` continued working while Loki was down.

## Collector restart test

A third Deployment emitted `KAN86_COLLECTOR_RESTART_PROBE` every second. Alloy
was restarted and reported ready. The Fluent Bit Pod was then deleted and
recreated by its DaemonSet. The source was removed and the result was queried
again after the final Alloy record had settled.

| Collection | Range | Unique records | Missing | Duplicates |
| --- | --- | ---: | --- | ---: |
| `fluent-bit` | 0–166 | 166 | 114 | 0 |
| `kubernetes-api` | 0–166 | 167 | None | 0 |

Sequence 114 appeared only in the Kubernetes API collection. An early query
temporarily lacked Alloy's final sequence 166, but that record appeared on the
settled query. The final observation is therefore one Fluent Bit gap and no
Alloy gaps or duplicates in this single trial.

## Live receipt-worker event

During a real OCR reprocessing run, the worker emitted a terminal event for
receipt hash
`0774172aab99fddba9b689b7b62b23db7ac0f23ae263d79c7bcfb8f2227cbc96`
with `status=review_required`. A Grafana datasource-proxy query filtered on the
`receipt-worker` container, exact hash, and exact status. Grafana returned the
same event from Pod `receipt-worker-65888f8744-tf226` in both the `fluent-bit`
and `kubernetes-api` collections. The API-collected form retained a trailing
newline; the application message content otherwise matched exactly.

This proves that both paths can carry and search a real application terminal
event through the user-facing Grafana query path, not only synthetic probes.

## Security and operational comparison

| Dimension | Fluent Bit node agent | External Alloy API collector |
| --- | --- | --- |
| Installation boundary | Requires a DaemonSet in the application cluster | No collector workload in the application cluster |
| Data access | Reads node `/var/log` through `hostPath` | Reads only Pods and `pods/log` in `home-budget` |
| Kubernetes identity | Namespace Role for Pod metadata | Namespace Role for Pod discovery and log reads |
| Secret access | Denied | Denied |
| Loki identity | Dedicated `fluent-bit-loki-client` certificate | Dedicated `alloy-loki-client` certificate |
| Local recovery state | Per-node tail database and filesystem chunks | Central WAL on monitoring host |
| Scaling cost | Collector CPU/memory/storage on every node | One external collector process; API and network cost grow with streams |
| Vendor-managed suitability | Not suitable when agents or node access are prohibited | Suitable if a stable API endpoint and least-privilege credential are available |

Both approaches add operational work beyond application logging. Fluent Bit
adds Kubernetes workload lifecycle, per-node storage, and node filesystem
privilege. Alloy adds external credential rotation, API reachability, and
dependence on terminated-Pod log availability. Both require Loki client
certificate rotation for this deployment.

## Interim findings

- Both paths deliver useful, bounded Kubernetes metadata and remain searchable
  with application identifiers kept in message content.
- Both paths returned the same real `receipt-worker` terminal event when
  filtered by its exact receipt hash and `status=review_required` in Grafana.
- Both paths recovered an exact record set after the controlled Loki outage.
- Both paths followed an application Pod replacement without loss in the
  measured window.
- The single collector-restart trial favored Alloy by one record, but the
  sample is too small for a general reliability conclusion.
- Fluent Bit used less memory in the observed single-node baseline, but its
  cost repeats on every node. The Alloy process used more memory but stays
  outside the application cluster.
- Method B satisfies the stated vendor boundary; Method A does not when
  in-cluster software or node-log access is prohibited.

Both methods are operationally viable for BrownRook-managed clusters based on
the functional checks in this comparison. This story does not select one as
the preferred managed-cluster collector because the deferred benchmark work is
needed for that decision. For vendor-managed clusters that prohibit agents,
the Kubernetes API path is the only tested design that satisfies the
installation boundary, subject to the vendor providing the required API access
and retention behavior.

## Deferred tests

The following measurements were left for future work and did not block closing
KAN-86 after the agreed Grafana verification:

1. Emit and compare a Python multiline traceback.
2. Repeat collector restarts enough times to estimate loss and recovery delay.
3. Interrupt Kubernetes API access and compare continued collection behavior.
4. Run a synchronized sustained-load window and capture CPU, memory, storage,
   network, delivery latency, and completeness from aligned counters.
5. Select a preferred collector for BrownRook-managed clusters if that decision
   becomes necessary.
