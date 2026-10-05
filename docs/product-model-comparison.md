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

## Collaborate using receipt evidence

After the GitOps release, run a bounded two-round collaboration:

```sh
make enrich-products COLLABORATE=1 LIMIT=1 DRY_RUN=1
make enrich-products COLLABORATE=1 LIMIT=1 RECEIPT='20260214_sobeys_363_95.pdf'
```

`COLLABORATE=1` defaults to one line item. It is separate from `COMPARE=1`;
select one mode per Job. The mode reads the latest stored OCR run for each receipt
source, including Tesseract, Paddle and any persisted vision-engine passes. It
retrieves probable matching lines, consensus text, line geometry, quantities,
prices, receipt totals and arithmetic checks. When asynchronous pass records lack
text, it reads the current run's recorded `ocr-cache` artifact from the mounted
cache directory. It verifies the recorded file size and SHA-256, source SHA and
reference, run UUID, and pass identity before recovering alternate text. Sources
retain their pass/run IDs and artifact URI/digest for audit and graph navigation.
It does not rerun missing OCR engines or silently substitute older OCR runs.

Only PVC artifacts beneath the configured cache root are supported by this
reader. Missing, overwritten, unsupported or invalid artifacts are reported in
`artifact_errors` and block automatic recommendations while leaving available
text usable for review. Old runs without a recorded artifact are not guessed
from filenames. The loader never writes recovered text back to PostgreSQL.

Round one gives each available model the same retrieved text and Brave baseline.
The baseline searches the original phrase and a bounded OCR C/O alternative.
Additional expansions are learned at runtime from the same retailer's accepted
`enrichment.product_cache` matches with confidence at least 0.9 and a product
page that still scores at least 0.9 against its original receipt text. For example,
an accepted `Oep Pic Med` → `Old El Paso Salsa Picante Medium` pair can teach the
brand initialism and the `Pic`/`Med` prefixes for later searches. There is no
fixed brand or abbreviation dictionary. Conflicting learned expansions are
bounded alternatives, not votes or proof of product identity. Review, rejected,
error, recipe, other-retailer and experimental model results cannot teach rules.

Learned searches record cache IDs, original text, product titles and URLs in
`learned_searches` in the logs and comparison/collaboration payloads. The graph
links these prior matches to the searches they generated. The history is reread
for each item, so accepting or revoking a cache match changes future searches
without a code change. Candidate scores still use the current original receipt
text; prior matches do not count as current receipt observations or independent
model votes. On an empty accepted cache, the system uses original/OCR-alternative
searches plus model-generated proposals; it does not invent learned mappings.
Vision profiles also receive up to two receipt page images, verified against the
source SHA-256 and read from the mounted receipt inbox. Text-only Qwen sees the
OCR evidence. Models propose readings and search queries with source citations.

The production collaboration profile uses `qwen3-vl:30b-a3b-instruct-q4_K_M` so Qwen receives the
same page images as OpenAI. Before syncing this profile, install the model on
Arsene (set `OLLAMA_HOST` to the managed service address):

```sh
OLLAMA_HOST=http://192.168.2.201:11434 ollama pull qwen3-vl:30b-a3b-instruct-q4_K_M
```

Large scanned PDFs up to 128 MiB are hash-verified in a stream and rendered
without loading the entire source into a Python byte buffer. Uploaded evidence
remains limited to two page images, each at most 2048 pixels per dimension.
Image failures include a machine-readable reason such as `source_too_large` or
`source_hash_mismatch`; source paths and credentials are not logged.

Model prompts group duplicate readings and use compact citation metadata. Full
artifact URIs, digests and pass provenance remain in the saved evidence bundle
and graph instead of being repeated for every observation in the prompt. The
character budget and request guard reserve 4096 tokens per attached image plus
the configured output budget; these are conservative estimates, not exact model
tokenization. Prompts request concise readings, explanations and citations.
Logs include `prompt_bytes`, `reserved_image_tokens`, and Ollama `finish_reason`
for incomplete outputs. A prompt-budget or truncation failure remains incomplete
and cannot become an accepted recommendation.
Brave searches those proposals; round two exposes every provider's proposal and
the ranked product candidates to every reviewer. Responses cannot cite unseen
sources or invent candidate IDs.

Collaboration tries Qwen first for discovery and review. A valid Qwen review can
produce a `recommended` result without an OpenAI call when original receipt scoring
reaches the threshold, a retailer product page and supporting OCR observation are
explicitly cited, both item and receipt arithmetic pass, required images are
available, and all relevant evidence is included without search or model errors.
Otherwise additional configured providers review the evidence and agreement across
at least two provider families is required; existing evidence checks still apply.
Qwen variants count as one family. Disagreements, abstentions, errors and weak
citations stay in `review`. Recommendations **do not overwrite** item names,
prices, totals, accepted products or product caches. This is evidence gathering
and reconciliation for review, not an automatic correction policy.

The new managed `receipt-model-profiles` ConfigMap supplies cloud model IDs and
Qwen budgets. OpenAI uses the existing key/model setting. Anthropic and Gemini
participate when the optional `anthropic-api` / `ANTHROPIC_API_KEY` and
`gemini-api` / `GEMINI_API_KEY` Secrets exist in `home-budget`. Missing credentials
are reported in `skipped_profiles`; they never become zero-confidence votes.
The defaults are pinned Claude Sonnet 4.6 and Gemini 3.8 Flash; change the model
IDs in Git for your accounts. Provider access and image support must be available.
See the official [Claude model reference](https://platform.claude.com/docs/en/models/sonnet-4-6/overview)
and [Gemini model catalog](https://ai.google.dev/gemini-api/docs/models).

Increase budgets or select multiple **already installed Arsene models**:

```sh
make enrich-products COLLABORATE=1 LIMIT=1 \
  CONTEXT_TOKENS=16384 OUTPUT_TOKENS=4096 \
  QWEN_MODELS='qwen3:30b,qwen3-vl:8b'
```

The default collaboration context/retrieval budget is 16,384 tokens and output
budget is 2,048 tokens, larger than the short product-query comparison. Qwen uses
these as `num_ctx` and `num_predict`; cloud providers receive the output limit and
a bounded evidence prompt. Context sizing for cloud providers is a retrieval
budget, not an API setting that changes their native context capacity. Images
also consume context. A conservative character check rejects oversized prompts;
it is not an exact provider tokenizer. Inspect actual usage and truncation errors.
Larger budgets are experiments, not guaranteed accuracy improvements.

Collaboration keeps distinct retrieved OCR readings as separate
`reading_hypotheses`, with their source IDs. It searches each reading on the
retailer site before model proposals, reusing cached queries. For example,
`Cep Pic Med` and `Oep Pic Med` remain separate hypotheses even when both occur
in the same OCR pass. Repeated passes do not add independent votes. Up to eight
distinct readings are searched; exceeding that budget records an error and keeps
the item in review. Hypotheses and search queries are retained in the run payload,
and hypotheses appear in `enrichment_collaboration_evidence` logs.

Models are asked to explore unresolved readings and abbreviation expansions;
there is no fixed mapping from Oep to a brand. Discovery results remain scored
against the original item. Review selections are constrained to supplied product
IDs or null, with local citation validation still enforced. A successful search
or model agreement alone does not approve a product.

Before the shared proposal/review rounds, each unresolved reading receives a
separate text-only expansion call, trying configured models in order until
discovery finds a strong retailer product match with supporting OCR. That call receives
only the target reading, its citations and the merchant, avoiding anchoring on
other OCR spellings or unrelated literal-search results. Queries retaining short
receipt tokens (up to four letters) are rejected; the model retries once with
full-expansion instructions, or explicitly returns no queries when it cannot
infer an expansion. The heuristic can also reject real short product words;
an unresolved expansion is recorded rather than accepted as a product fact.
Expanded queries are pooled and deduplicated for Brave verification, with scores
still calculated against the original receipt item. Only product pages can be
selected in the final review; brand indexes and store pages remain discovery
evidence. Calls run sequentially, so ambiguous items take more time and model
usage when discovery remains unresolved. Brave checks expansions as they arrive;
once a strong match is found, remaining expansions and the shared proposal round
are skipped. Qwen reviews the pooled evidence first. Additional model reviews run only when
the Qwen-only recommendation checks are not satisfied. If literal Brave results
already supply a supported product page, discovery model calls are skipped too.
The decision records `review_policy.skipped_profiles` and `fallback_reasons` so
paid-model use or omission can be inspected in logs and the graph. `enrichment_collaboration_discovery` records the
stop reason, expansion call count and whether the proposal round was skipped.

Qwen receives the JSON schema in its prompt as well as Ollama's `format` argument.
Expansion reading/query strings are limited to 160 characters, reasons to 300,
and citations to six. Partial queries containing receipt abbreviations are searched
before requesting corrections. Only retailer product pages with the existing score
and independent OCR support can stop discovery; a partial model guess cannot.
If those queries do not verify, Qwen moves to the next OCR reading instead of
spending another reasoning call on the same partial expansion. Other providers
retain one semantic correction attempt, and a failed correction preserves the
valid first response and both attempt records.
Production uses the explicitly named Instruct model rather than the ambiguous
`30b` tag. Thinking-only truncations (no answer content, nonempty thinking)
skip the larger-budget retry and record `retry_skipped_reason`; expansion then
opens its existing provider circuit and allows fallback discovery. Review also
skips that unproductive retry. Other truncated responses get one concise retry. Qwen recovery requests may
double the output budget, capped at 16384 tokens and the context ceiling.
A retry is skipped when the budget cannot grow; the existing context and image
budget guard still applies. Default Qwen profiles use 32768 context tokens and
8192 output tokens, independently of OpenAI's 2048-token output default.
Thinking and the final answer share Ollama's output budget. Override only Qwen
with `make enrich-products COLLABORATE=1 LIMIT=1 QWEN_CONTEXT_TOKENS=32768 QWEN_OUTPUT_TOKENS=8192`.
Shared `CONTEXT_TOKENS`/`OUTPUT_TOKENS` settings still apply to all defaults unless
the Qwen-specific values are supplied; explicit JSON profiles retain their budgets.
Larger contexts use more GPU memory and may cause CPU offload on the 24 GB card.
If both attempts truncate, expansion calls stop for that Qwen profile rather than
repeating failures across every remaining OCR reading. Other configured profiles
can continue discovery. Qwen reviews also get one bounded truncation retry.
Truncated Ollama responses include `incomplete_output` in the model-result log
and saved attempt history. `content_head` and `content_tail` retain at most 1024
response characters combined (768 from the beginning and 256 from the end for
long responses); `content_chars` and `omitted_content_chars` describe the full
response length without storing its middle. `thinking_chars` counts returned
thinking text without retaining that text; null means Ollama did not return a
string in that field and does not prove that no reasoning occurred. Request
bodies and attached images are not copied into these diagnostics. Token usage,
`finish_reason`, requested output budget and Ollama timing remain available for
comparison. Inspect the sample for repetition or unfinished JSON before raising
budgets again. Historical runs cannot recover response text that was discarded.
Incomplete responses are never parsed as valid evidence. Persistent failures
remain recorded, including failures later superseded by verified discovery and
a successful review.
Reviews require `candidate_title` to exactly match the retailer title for
`candidate_id`; both fields must be null when abstaining. The title schema is
restricted to supplied retailer titles, and title/ID mismatches require a fresh
review. Unverified expansion names are retained in discovery history but are
not repeated as product identities in the review summary.
`product_source_id` must equal `candidate_id` (both null when abstaining); `source_ids` must cite supporting OCR evidence. The schema restricts
both product fields to supplied candidate IDs. Missing or mismatched product
citations or missing supporting OCR citations get one new model review. Both attempts remain recorded; citations are never inserted
by the application. Unresolved evidence failures continue to block recommendations.

An expansion `IncompleteModelOutput` can be classified as recovered only after
verified retailer discovery, complete evidence, passing item and receipt
arithmetic, and a valid, cited review from that same model profile. The review
must meet the normal product-page, score, OCR-citation and title checks. Other
search/model failures, missing evidence and failed reviews remain blocking.
The original `errors` list is retained; `recovered_errors` and `blocking_errors`
explain their effect on the final decision. Run `incomplete` counts use blocking
errors, so successful recovery does not fail the Job. Logs and graph decision
properties retain the recovered discovery failures.

A model-generated quoted Brave search with no results gets one retry after
removing phrase quotes. Every search term and the retailer site restriction remain;
the broader query uses the original receipt item for scoring. Successful quoted
searches are not broadened. Both queries are cached and retained, with
`query_fallbacks` and `enrichment_collaboration_search_fallback` recording the
reason. Search errors remain errors and do not masquerade as empty results.

Inspect `enrichment_collaboration_expansion` for `target_reading`, `attempts`,
`expansion_state`, queries and GPU usage. The payload retains expansions, and
the knowledge graph links their proposals to observations and actual searches.
This restores the successful August 22 interactive method: OpenAI proposed
`Old El Paso medium picante salsa`, Brave returned the Sobeys product page,
and the former enrichment flow accepted it. That earlier run used OpenAI;
Qwen now participates in the same expansion process. Historical acceptance
does not override the evidence requirements of a new collaboration run.

Models on your Mac are not automatically available on Arsene. The Job never
pulls models. Qwen profiles containing `vl` are treated as vision capable by the
default configuration; use explicit profiles for custom model names. GPU requests
run sequentially and unload each model after its call. The managed Ollama host
limits loaded models and parallel requests to one. Apply the updated host role
with `make ollama-gitops-check` / `make ollama-gitops-apply`. No new replica count or
worker CPU allocation is required.

For different context sizes of the same Qwen model, set `RECEIPT_MODEL_PROFILES`
in the ConfigMap to JSON with unique profile names. An explicit list replaces all
default profiles, including the cloud providers:

```json
[
  {"name":"openai","provider":"openai","model":"gpt-5.6-terra","vision":true},
  {"name":"qwen-8k","provider":"qwen","model":"qwen3:30b","context_tokens":8192,"output_tokens":2048},
  {"name":"qwen-16k","provider":"qwen","model":"qwen3:30b","context_tokens":16384,"output_tokens":4096},
  {"name":"qwen-vision","provider":"qwen","model":"qwen3-vl:8b","context_tokens":16384,"output_tokens":2048,"vision":true}
]
```

At most eight profiles participate, with two requests per item and no automatic
provider retries. Each profile can propose up to three searches. Calls are
sequential to bound service/GPU load; cloud and Brave calls can incur charges.
The source selection is bounded to four documents, 80 latest-run OCR passes and
relevant item excerpts. Repeated readings are deduplicated while preserving their
pass citations. All included sources, search results, proposals, reviews, usage,
image hashes and omissions remain in `enrichment.receipt_collaborations.payload`;
run/receipt timing is in `enrichment.receipt_collaboration_runs.summary`.

Grafana adds **Collaborative decisions and disagreements**, **Collaborative
proposals and reviews**, and **Shared OCR and image evidence coverage**. Apply
with the monitoring GitOps targets and filter by the new run UUID. The GPU timeline
and receipt timing panels also include collaboration. In Loki Explore:

```logql
{namespace="home-budget", container="enrich"}
| json
| event=~"enrichment_collaboration_.*"
```

```sql
SELECT run_uuid, expense_item_id, completed_at,
       payload->'decision' AS decision,
       payload->'proposals' AS proposals,
       payload->'reviews' AS reviews,
       payload->'evidence_bundle' AS evidence
FROM enrichment.receipt_collaborations
ORDER BY completed_at DESC;
```
