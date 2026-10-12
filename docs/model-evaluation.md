# Receipt model evaluation

Develop on the Mac and compare models through Arsene's Ollama API. This is a
read-only, image-based extraction experiment: it uses the production image
renderer and structured Ollama request client, but its extraction prompt is an
evaluation prompt. It does **not** run the entire production OCR, product search,
collaboration, category rules or persistence workflow. An improvement here is a
reason to test the model in those workflows, not proof of an end-to-end gain.

## Run a comparison

Both models must already be installed in the **managed LAN Ollama service**.
The model store used by a separate `ollama serve` session on Arsene can differ.
The evaluator checks installed models and vision capabilities before inference;
it never pulls, deletes, configures or unloads models.

```sh
# This explicitly installs the candidate in the same service the pipeline uses.
OLLAMA_HOST=http://192.168.2.201:11434 ollama pull qwen3.8:27b

OLLAMA_BASE_URL=http://192.168.2.201:11434 make eval-models \
  BASELINE=qwen3-vl-receipts:30b-instruct CANDIDATE=qwen3.8:27b
```

The default case uses your private Sobeys PDF at
`.receipt-test/inbox/2026-02-14/20260214_sobeys_363_95.pdf`. Override its root with
`RECEIPT_ROOT=/path/to/inbox`. The default case includes both pages of that PDF.
Both models receive exactly the same rendered
images, extraction prompt, JSON schema, context and output budgets, with thinking
disabled and temperature zero. These settings override model sampling defaults.
The context defaults to 32768 and output to 8192 tokens, matching the current
Qwen collaboration budgets; lower both with `EVAL_CONTEXT_TOKENS` and
`EVAL_OUTPUT_TOKENS` when deliberately testing a smaller memory budget.

Runs are sequential, grouped by model to reduce model reloads. Three repeats
per case expose variation; override `EVAL_REPEATS=1` for a quick check. Run when
other inference jobs are idle: sequential requests from this command do not
prevent production or Jupyter from sharing the GPU. Loading the candidate may
evict the existing model from memory and affect other request latencies.

## Read the results

Each run writes `.model-evals/<UTC-time-and-run-id>/report.md` and `report.json`.
The reports are ignored by Git and created with private permissions. JSON retains
model outputs and verified labels, so treat it as receipt data. It excludes image
payloads and reasoning text. It records Git revision and dirty state, Ollama
version, model digests and template hashes, input/rendered image/reference hashes,
prompt/schema hashes, settings, per-request timings, token counts, GPU samples,
and loaded model placement.

The Markdown report shows verified summary matches, known purchase rows matched,
median wall time, repeat consistency and discrepancies. JSON provides every
output and comparison for review. Load/prompt/generation durations are separate
from wall time: the first request may include a cold model load, and later
requests may reuse prompt caches. GPU measurements cover the whole host and do
not attribute memory or activity exclusively to the evaluation.

Quality differences, missing items and arithmetic discrepancies are warnings;
they do not make the command fail or select a winner. A missing model/input fails
preflight (exit 2). API errors, truncated output, invalid extraction contracts or
unverifiable/changing model digests produce an incomplete evaluation (exit 1).
Reports are saved after every request, and the remaining requests still run.
Interrupted runs retain their partial report, without a finished timestamp.
These live comparisons are manual and are not required GitHub CI gates.

## Reference coverage

The existing Sobeys regression fixture supplies merchant, date, total, payment
method and known item names/prices. It is a **partial** reference: tax, subtotal,
full item count and categories are unscored. Extra extracted rows are retained
for review and are not called hallucinations. Matching preserves duplicate
purchase counts and accepts only explicitly listed name aliases plus exact
currency amounts, rounded to cents. This is strict label agreement, not a
semantic accuracy estimate; inspect spelling differences before judging them.
Arithmetic checks are informational because receipts may include additional
fees or rounding.

Add cases to `evals/receipts/cases.json`. Private images stay outside Git. A case
can specify direct verified labels instead of `expected_fixture`:

```json
{
  "id": "verified-example",
  "split": "holdout",
  "source": "example.pdf",
  "pages": [1],
  "complete_items": true,
  "expected": {
    "merchant": "Example Store",
    "total": 11.30,
    "subtotal": 10.00,
    "tax": 1.30,
    "items": [
      {"item_name": "MILK", "line_total": 10.00, "category": "Groceries"}
    ]
  }
}
```

This illustrative case is not an actual answer key. Verify labels from the source
receipt before using them. Include category only when a person has verified it
against the application's category catalog. Mark `complete_items=true` only after
verifying all rows and duplicate counts. Choose one or two page numbers; add
separate cases for longer receipts rather than silently dropping pages. Unknown
fields should be omitted from the reference; null explicitly labels an absent
field. Select a separate case manifest with `EVAL_CASES=/path/to/cases.json`.

## Qualification and promotion

1. Compare the candidate with the current model using unchanged prompts/settings.
2. Review errors on discounts, taxes, returns, poor scans and duplicate rows.
3. Tune candidate prompts/settings on a branch, retaining the initial report.
4. Evaluate a separately maintained holdout receipt set before promotion. The
   `split` label is recorded for review; the evaluator does not enforce isolation.
5. Exercise actual production collaboration/product enrichment for the candidate
   explicitly, including vision-enabled profiles (model names alone are not a
   reliable way to infer vision support).
6. Review quality and resource costs, then commit the chosen model digest,
   settings and a sanitized evaluation summary through the normal GitOps process.
   Keep the previous configuration for rollback. Do not commit private reports.

A single Sobeys receipt cannot qualify a model for production. It gives an
initial comparison and a reproducible place to investigate differences.
