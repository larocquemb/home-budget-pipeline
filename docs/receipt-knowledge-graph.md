# Receipt knowledge graph (KAN-98)

The authenticated Ledger page `/ledger/graph` shows how receipt workers, OCR
methods, model proposals, Brave searches and reviews contribute to a result.
The default **Processing story** follows one item and collaboration run from OCR
and receipt evidence through model expansion, Brave results and model verification
to the final recommendation. Green borders highlight recorded supporting citations;
expand alternatives to inspect other attempts. Item and run selectors keep older
attempts separate. Completion timestamps select the latest run, including failures;
older projections without timestamps explicitly report unknown completion order.
The summary shows recorded token evidence, arithmetic checks, item duration and
whether the canonical record changed. Model explanations remain reported claims.

Three additional perspectives share the same evidence: receipt lineage and
collaboration, operational topology, and merchant/item/category relationships. Select a node
for its evidence, usage, timing, citations and links; use **Explore related
receipts** to investigate a worker, method, model, merchant or category.

## Evidence and interpretation

Blue nodes describe processing or infrastructure, purple nodes describe methods
and models, amber nodes describe observations/proposals/search results, and green
nodes describe canonical records. Red indicates failures or decisions requiring
review. A model recommendation never creates an accepted product or changes PostgreSQL.
The receipt page displays the latest saved collaboration per line item, even when
no accepted enrichment exists. **Accept recommendation for this item** applies the
clean product name and URL after checking the receipt, current item fields and
latest collaboration. It preserves the OCR item name, amounts and established
categories; supported category rules resolve uncategorized items. The
transaction records the user and old/new values in the description audit, marks
the product enrichment accepted, updates the accepted search cache and appends a
`product_recommendation_accepted` lineage event. Repeated clicks are idempotent;
stale or ineligible recommendations cannot overwrite newer item descriptions.

The receipt-wide **Apply accepted matches and category rules** action repairs
older uncategorized rows and fills empty products from accepted exact retailer/text
cache matches after rechecking product-page evidence. Per-item collaboration reviews
and existing descriptions are preserved. Each change records a `receipt_item_refreshed`
lineage event, projected as an **ItemRefresh** with its audit record.
After projection, an explicit `ProductAcceptance` connects the decision to the
canonical item and its audit through `ACCEPTED_AS`, `UPDATED` and `PERSISTED_AS`.
The original model payload remains unchanged.
The graph connects each review to the proposals it considered, the observations
it cites, and its guardrail/reconciliation decision. The decision properties
explain disagreements, missing evidence, insufficient independent providers,
truncated context and arithmetic failures.

OCR runs/passes and collaboration observations retain their run identities.
Identical text from separate runs remains separate historical evidence. Search
result title/snippet/scoring snapshots are specific to a collaboration run, while
the referenced page URL connects runs. Image nodes contain hashes and references,
never image binaries. `RECEIVED` links require recorded prompt source IDs or actual
image exposure; stored evidence is not automatically presented as model input.
Invalid citations are explicit unresolved/rejected links, not supporting facts.
GPU summaries identify the sampled inference host/GPU, which may differ from the
worker node. Missing GPU samples and historical provenance remain unknown.

The source schema is `lineage.receipt_events`, `budget.receipt_ocr_runs`,
`budget.receipt_ocr_passes`, canonical expense/item records, and
`enrichment.receipt_collaborations`/`receipt_collaboration_runs`. PostgreSQL is
authoritative. New confirmed publications and worker attempt/outcome observations
are persisted without any Neo4j call from the worker. Evidence persistence errors
emit `lineage_evidence_unavailable` and do not turn successful receipt work into
failure. Investigate these errors: the graph cannot reconstruct an event that
was never retained. Existing OCR runs do not prove the identity of an original
work message or a prior failed attempt; the projector does not invent those links.

## Projection and replay

Neo4j Community 5.26.31 provides the derived graph. The visualization uses SVG
and browser JavaScript inside the existing authenticated FastAPI application,
without a third-party CDN or database credentials in the browser.

`ReceiptFlowEntity` nodes have a unique stable `id`, `kind`, display `label` and
`properties_json`. Relationships use explicit types such as `EXECUTED_BY`,
`RUNS_ON`, `HAS_PASS`, `USES_MODEL`, `PRODUCED`, `CITES`, `SUPPORTS`,
`CONSIDERED_PROPOSAL`, `RESULTED_IN`, `RECOMMENDS`, `RETRIED_AS`, `CONTAINS`,
`CLASSIFIED_AS` and `PERSISTED_AS`. Model IDs include provider and model name;
and recorded digest where available; profile/context settings belong to individual invocations, so multiple Qwen
profiles do not imply independent providers. Observation/candidate IDs include
run and item identity. Processing attempt UUIDs distinguish redeliveries.

Each `ReceiptSnapshot` contains the atomic graph used by the UI and `HAS_ENTITY`
membership for bounded cross-receipt navigation. The projector reads a repeatable
PostgreSQL snapshot, then commits all graph changes in a Neo4j transaction. A
receipt lock and source-snapshot timestamp reject older racing snapshots.
Replaying the authoritative history merges stable entity/relationship IDs and
does not duplicate them. Retained entity relationships can include past domain
correlations; use the current `ReceiptSnapshot.graph_json` for current canonical
classification. Canonical reprocessing can delete historical PostgreSQL rows;
retention must preserve authoritative history for complete rebuilds. This slice
does not invent history deleted before projection or provide an archival store.

The scheduled projector scans all receipts in batches every ten minutes and
reports its last completed batch cursor. Repeated scans include new events for
existing receipts. The next run retries after a backend failure; receipt workers
and canonical persistence continue. Manual replay:

```sh
make receipts-graph DRY_RUN=1
make receipts-graph
make receipts-graph SHA=<64-character-source-sha>
```

For direct administration inside the application image, use
`home-budget-receipt-graph --all` or `--limit 100 --after <last-sha>`.
Avoid simultaneous manual runs while the scheduled job is active: stable IDs
and source timestamps protect consistency, but extra scans waste resources.

## GitOps deployment

The opt-in overlay is `deploy/receipt-graph`, built on the current production
`deploy/rabbitmq-private-telemetry` overlay, preserving collectors and log delivery.
It provisions the graph database on Arsene with 1Gi requested / 2Gi limited
memory, a 1-CPU limit and an 8Gi local-path volume. The projector requests 100m
CPU / 128Mi memory and has a 512Mi memory limit. These are initial budgets,
not demonstrated performance limits; measure graph size and scan duration.
The graph has no GPU reservation and must not crowd OCR or Ollama memory.

Create the uncommitted `neo4j-secret` through the approved secret workflow before
selecting the overlay. `make receipts-graph-secret` creates the initial secret
with a random password through a temporary owner-only file, prints no credentials,
and preserves an existing secret. Store/recover the credential through the
approved secret management process. Required keys:

| Key | Value |
| --- | --- |
| `NEO4J_AUTH` | `neo4j/<strong-password>` for first database initialization |
| `NEO4J_URI` | `bolt://neo4j.home-budget.svc.cluster.local:7687` |
| `NEO4J_USER` | `neo4j` |
| `NEO4J_PASSWORD` | The same password, supplied securely |
| `NEO4J_DATABASE` | `neo4j` |

Do not place these values in Git, paste them into chat or pass passwords through
receipt/job logs. Neo4j initialization credentials do not rotate an existing
database password. Community edition does not provide Enterprise fine-grained
role separation; the server-side reader/projector share the managed account in
this initial deployment. Restrict access with the network policy and fixed
authenticated read endpoints. Use an appropriately licensed deployment with
separate least-privilege credentials if stronger role separation is required.

```sh
kubectl kustomize deploy/receipt-graph
make receipts-graph-secret
```

In `brownrook-infra/kubernetes/gitops/apps/ledger-app.yaml`, change
`spec.source.path` to `deploy/receipt-graph`, then merge that change. The
`brownrook-root` parent owns the Ledger Application and reverts direct
`argocd app set` overrides. After merging, reconcile the parent before syncing
Ledger:

```sh
argocd app sync brownrook-root --server argocd.brownrook.net --grpc-web
argocd app get ledger --server argocd.brownrook.net --grpc-web
make deploy-k3s
kubectl --context brownrook-k3s1 -n home-budget rollout status statefulset/neo4j
make receipts-graph
```

Use the application image built from this change. The existing database bootstrap
applies the additive lineage schema before new workers use it. Neo4j remains
ClusterIP-only; its network policy allows Bolt only from Ledger and the projector.
The web service account can list pods only in its own namespace. Existing OAuth
proxy authentication protects graph APIs. No public Neo4j ingress is added.

## Health, logs and troubleshooting

Worker health overlays use current Kubernetes Pod Ready conditions, refreshed
every 30 seconds with a 90-second stale threshold. An absent historical pod has
unknown current health, rather than being classified as failed. These overlays
do not prove active receipt processing. Historical attempt/model outcomes and
timings remain independently visible.

Optional `GRAPH_PROMETHEUS_URL` enables fixed Prometheus target-health queries
where the server already has approved access. Prometheus on the monitoring host
listens on loopback by default: do not expose it to the LAN just for this view.
Missing metrics access is displayed as unavailable. Queue depth, processing
latency and detailed metric panels remain accessible through Grafana; direct
queue-depth/latency overlays are not part of this first canvas implementation.

Default Grafana links use `https://grafana.idc.brownrook.net`, Loki datasource UID
`cfy4qg8i178jkb`, and Tempo UID `home-budget-tempo`. Override
`GRAPH_GRAFANA_URL`, `GRAPH_LOKI_UID`, `GRAPH_TEMPO_UID` for another deployment.
Receipt/run/worker identifiers filter Loki where available. Verify deployed
links and backend retention: a recorded trace identifier does not guarantee a
retained Tempo trace. No additional model/search calls happen when opening a view.

```sh
kubectl --context brownrook-k3s1 -n home-budget get cronjob receipt-graph
kubectl --context brownrook-k3s1 -n home-budget logs -l app=receipt-graph --tail=100
kubectl --context brownrook-k3s1 -n home-budget logs -l workload=receipt-worker --tail=100
```

A graph 503 leaves receipt processing available. Check Neo4j readiness, secret
configuration, policy selectors and projector failures. A receipt missing from
search may not have been projected yet. The UI returns at most 200 nodes per
request and paginates; connected paths outside a page are not displayed until
selected. This is a bounded neighborhood view, not a claim that no other evidence
exists.

## Investigations and recovery

Use Neo4j Browser through an authenticated administrative port-forward when
needed. Do not expose database ports publicly. Example bounded queries:

```cypher
// Worker/model/category impact: select an entity ID from the UI.
MATCH (s:ReceiptSnapshot)-[:HAS_ENTITY]->(n:ReceiptFlowEntity {id: $entity_id})
RETURN s.sha, s.name LIMIT 100;

// Evidence contributing to a decision.
MATCH p=(review:ReceiptFlowEntity)-[:INFORMS]->(decision:ReceiptFlowEntity)
WHERE decision.kind = 'Decision'
RETURN p LIMIT 100;

// Retry branches.
MATCH p=(a:ReceiptFlowEntity)-[:RETRIED_AS]->(b:ReceiptFlowEntity)
RETURN p LIMIT 100;

// Recommendations and review-required outcomes (details in properties_json).
MATCH (d:ReceiptFlowEntity {kind: 'Decision'})
RETURN d.label, d.properties_json LIMIT 100;
```

Neo4j is rebuildable only to the extent that PostgreSQL retains the source
evidence. Back up PostgreSQL evidence with its existing procedures. For a graph
backup, suspend the projector, stop Neo4j cleanly and snapshot its PVC using the
cluster storage backup procedure; restore to a compatible pinned Neo4j version
and verify authentication before resuming. A single local-path volume is not HA.
For rebuilds, provision a fresh empty graph database and replay all authoritative
receipts. Do not delete a production graph/PVC as a routine replay operation.
Roll back to the previous Argo source path after stopping the projector and
preserving its PVC/evidence; graph availability is not a prerequisite for receipts.

## Completion evidence still required

KAN-98 remains In Progress until deployed successful, failed/retried, cache-only
and collaboration demonstrations, real evidence-link checks, resource measurements,
outage/catch-up and fresh-database replay are recorded. This implementation does
not claim that current status overlays include every metric, that unavailable
historical queue identities are reconstructed, or that a model recommendation is
the accepted best product. The deployment commands are for the operator to run.

## Product labels and OCR variants

For the receipt-page review and acceptance workflow, see
[Understand item readings and product recommendations](user-guide.md#understand-item-readings-and-product-recommendations).

Product names omit leading shopping prompts (Buy, Shop, Purchase, Order) and
a matching retailer suffix such as `| Sobeys Inc.`. The original webpage title
stays in the saved candidate and recommendation evidence. New acceptance saves
the clean name; existing accepted raw titles display cleanly without silently
rewriting canonical data or manual descriptions.

Receipt rows lead with the **accepted interpretation** when a product-based
initialism correction has been accepted. The text selected during import stays
in collapsed **Import history**. Before acceptance, the imported text remains the
main label and any correction is labelled **Proposed interpretation**.
**Related OCR observations** lists actual
observations retained in that item's collaboration, grouped by reading with
engine, pass, variant, run and original line text. Canonical text and model
hypotheses are excluded from this list; a correction is never presented as a
new OCR observation. Repeated observations remain inspectable without treating
their count as independent agreement.

OCR text retrieval identifies related lines, not an exact canonical item row.
The retained canonical items have no per-pass bounding box or source-line link;
OCR output line numbers must not be interpreted as Ledger item numbers. This is
especially ambiguous for duplicate descriptions and amounts. The receipt page
and graph label these observations **row association unverified**, including old
collaborations. New retrieval metadata and model prompts retain this uncertainty.
Product-based corrections remain separate from observed OCR. Exact row attribution
requires an explicitly retained item-to-source-position mapping; text similarity,
matching amounts or pass line order alone do not establish it.
