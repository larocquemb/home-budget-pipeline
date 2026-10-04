# Compare OpenAI and Qwen product matches

Use `make enrich-products COMPARE=1` to run both models on the **same database
receipt items**. This is product enrichment after OCR, not a comparison of OCR
engines. It calls OpenAI and Arsene's Ollama service on every selected item,
including items already enriched. It does not wait for the normal AI fallback.

The comparison first searches Brave using the receipt text and a C/O OCR
variant where applicable. Both models receive the same top five unique retailer
results (titles, URLs and snippets), identical prompts and JSON schemas, and must
propose one to three expanded searches. This brings the comparison into line with
the normal enrichment path, which already searched Brave before its AI fallback. An empty response gets one retry with a shared clarification;
two empty responses are recorded as a model-output error with null confidence,
not a zero-score product match. Neither provider borrows the other's proposals.
Brave retrieves retailer evidence. The scorer evaluates all candidates against
the **original item text**, not the model's expanded words. Identical queries
share their search response within an item. Each provider can select from the
shared baseline and its own proposed searches, never the other provider's new
candidates. `candidate_origin`, `baseline_confidence` and `confidence_gain` show
whether a model improved on the baseline; ties can simply preserve that baseline.

Scoring version `receipt-evidence-v2` records per-token evidence: exact words
weigh 1, word beginnings 0.9, brand initials 1, a single C/O OCR ambiguity in
brand initials 0.95, and sauce/salsa category synonyms 0.85. It averages these
weights; any unmatched meaningful token caps the score at 0.84. It requires a
retailer product page and at least two meaningful tokens, or an exact barcode.
Recipes, brand listings and categories score zero. It no longer matches `Pic`
inside `spice`. Normal enrichment also uses this scorer and rechecks cached
candidates against the original receipt text before accepting them.

Confidence remains a lexical evidence heuristic, **not a calibrated probability
of correctness**. It does not verify package size or purchase identity. Review
URLs, titles, snippets and the `evidence.tokens` explanation alongside scores.
Prompt version `product-queries-v3` adds the shared Brave context; filter by run
UUID when comparing experiments rather than mixing scores from earlier versions.

The job records append-only pairs in `enrichment.product_comparisons` and emits
structured logs for Grafana. It does not change receipt items, accepted product
cache entries or enrichment audit records. Provider/search failures are recorded
with null confidence and an incomplete pair; they do not count as model wins.
The job finishes with a nonzero exit status if any pairs were incomplete.

## Deploy the comparison mode

Merge and deploy the application changes through the normal GitOps release
workflow. The existing database PreSync upgrade creates the comparison and run-summary tables;
the managed `product-enrichment` CronJob supplies the application image and
database, OpenAI and Brave secret references. It remains suspended, so comparisons
run only when requested. The OpenAI model uses the existing `AI_PRODUCT_MODEL`
setting/default. Qwen uses `OLLAMA_PRODUCT_MODEL=qwen3:30b` and
`OLLAMA_BASE_URL=http://192.168.2.201:11434` from the CronJob manifest.

Apply the Grafana dashboard from this repository on your Mac:

```sh
make monitoring-gitops-check
make monitoring-gitops-apply
```

The dashboard is **Home Budget Product Model Comparison**, in the private Home
Budget folder at
[Grafana](https://grafana.idc.brownrook.net/d/home-budget-enrichment-comparison).
It uses the existing Loki datasource and Fluent Bit logs. Select a time range
covering the experiment; paste its `run_uuid` into the dashboard filter to isolate
one run. Logs are limited by Loki retention, while the database pairs remain.

If panels show no data, set the time range to **Last 6 hours** and the Run UUID
filter to `.*`. In Grafana Explore, select Loki and check the raw events:

```logql
{namespace="home-budget", container="enrich", collection="fluent-bit"}
  |= "enrichment_model_result"
  | json
```

Fluent Bit emits the application JSON directly. The dashboard also handles
wrapped `message` JSON by unwrapping only when that field exists. If Explore
finds no events, check log collection before rerunning the experiment.

## Make Ollama reachable

On Arsene, inspect the listener:

```sh
ss -ltnp | grep 11434
```

The job must reach the service from Kubernetes. Manage Arsene's listener and
source-restricted firewalld rules through the Git-controlled Ansible playbook:

```sh
make ollama-gitops-check
make ollama-gitops-apply
```

Run these Make commands on your Mac. The role configures the existing service at
`192.168.2.201:11434`; when firewalld is active it allows the private LAN and K3s
pod network in the default zone. For a dedicated LAN zone, set
`ollama_firewall_zone` in `ops/ollama/inventory/production.yml`. Other firewall
implementations require their own managed rule. Keep the unauthenticated endpoint
on the private network. Check reachability from your Mac before running the job:

```sh
curl --fail --max-time 5 http://192.168.2.201:11434/api/tags
```

Qwen loads automatically on the first request and stays loaded for five minutes
after requests. Model calls time out after 180 seconds, including cold loading.
The job runs providers/items sequentially and uses one Ollama model instance.
The caller Pod needs no GPU allocation; Ollama on Arsene owns the RTX 3090.
Check actual offload on Arsene during the experiment:

```sh
export OLLAMA_HOST=http://192.168.2.201:11434
ollama ps
watch -n 1 'ollama ps; nvidia-smi'
```

The CLI defaults to localhost. Set `OLLAMA_HOST` to the managed LAN listener;
do not start a second `ollama serve` process to inspect the running service.

The role also installs a private read-only GPU telemetry service at
`192.168.2.201:11435/gpu`. It runs a fixed `nvidia-smi` query as the existing
Ollama service user; the same source restrictions cover both ports. Apply the
host role before running the new application image:

```sh
curl --fail --max-time 5 http://192.168.2.201:11435/gpu
```

During Qwen requests, the job polls that endpoint once per second and emits
`enrichment_gpu_sample` with receipt, item and run identifiers. Model results
include average/peak GPU utilization, sample coverage, sampling failures and
approximate observed active seconds. Missing samples remain unknown, not zero.
These are **host GPU observations during the request**, so concurrent workloads
can contribute. Active seconds estimate sampled activity, not CUDA kernel time.
The memory panel still reports allocation from Ollama `/api/ps` `size_vram`.
Ollama's own `load`, `prompt_eval`, `eval` and `total` durations are recorded in
seconds, accumulated across retries. The job requests a 4096-token context by
default (`OLLAMA_CONTEXT_TOKENS`), with a 1200-token output budget.

## Run a bounded comparison

Run these on your Mac after deployment. Start with one item:

```sh
make enrich-products COMPARE=1 LIMIT=1 DRY_RUN=1
make enrich-products COMPARE=1 LIMIT=1
```

Select a receipt filename or database receipt ID:

```sh
make enrich-products COMPARE=1 LIMIT=10 \
  RECEIPT='2026-08-14/receipts_20260814_0001.pdf'
```

`LIMIT` caps the selected line items even for a receipt. Comparison defaults to
10 items; normal enrichment keeps its existing default of 100. Each comparison
item normally uses two model calls and up to eight Brave searches (two shared
baseline queries and up to three per model, with identical queries reused). Each provider
may retry an empty response once; token totals and latency include that retry.
OpenAI and Brave usage
can incur charges. Run one experiment at a time to keep GPU and service load
bounded. The Make target follows logs and reports completion/failure; Ctrl+C
stops log following but leaves this finite Job running.

The dashboard shows evidence scores, accepted matches at the 0.85 threshold,
acceptance rates, score wins/ties, exact product URL agreement, model response
times (including cold load), observed Qwen GPU memory and utilization, sampling
errors, Ollama evaluation time, receipt enrichment time and per-item paired evidence. Failed providers are excluded from score and acceptance summaries.
Model latency excludes Brave searches so shared-search cache reuse does not
favor the second model.

To inspect durable results, run this SQL using your usual database client:

```sql
SELECT run_uuid, expense_item_id, compared_at,
       payload->>'winner' AS winner,
       payload->>'agreement' AS agreement,
       payload->'results' AS provider_evidence
FROM enrichment.product_comparisons
ORDER BY compared_at DESC, expense_item_id;
```


`enrichment_receipt_timing` reports total enrichment time for the selected items
in each receipt, with shared Brave and provider breakdowns. `complete_receipt`
is true only when every database line item was selected. `LIMIT=1` usually means
a partial receipt; use a sufficiently large limit with `RECEIPT` for a whole
receipt. These durations exclude queue waiting and OCR. OCR already records
`processing_seconds` and extraction/structuring stage timings in its receipt
cache metadata; the new duration measures the subsequent comparison stage.
Run summaries are persisted in `enrichment.product_comparison_runs.summary`:

```sql
SELECT run_uuid, completed_at, summary->'receipts' AS receipt_timings
FROM enrichment.product_comparison_runs
ORDER BY completed_at DESC;
```
