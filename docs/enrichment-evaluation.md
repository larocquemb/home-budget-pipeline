# Informational production collaboration evaluation

Run the test locally on your Mac, with model inference on Arsene:

```sh
make eval-enrichment BASELINE=qwen3-vl-receipts:30b-instruct CANDIDATE=qwen3.8:27b
```

The default workflow now calls the **production `receipt_collaboration.collaborate_item`
function**, not the older paired query-expansion experiment. It uses the existing
brand/descriptor expansion, suffix searches, full-word retry policy, candidate
verification, evidence packing, citation review and reconciliation. No OEP-specific
answer or glossary is supplied to either model. Stage prompts, schemas and retry
settings are the production implementations; descriptor expansion uses its existing
seeded temperature settings, not a replacement evaluator prompt.

## Real input evidence

The evaluator selects the Sobeys salsa item from the database configured by your
local `.env.dev`, using the receipt filename and canonical item aliases in
`evals/enrichment/cases.json`. It calls the production shared evidence loader to
read the latest OCR alternatives, line geometry, source hashes and arithmetic
checks. Selection deliberately includes previously accepted items for evaluation.
Both models get independent deep copies of the **same database snapshot**. They
are each tested as one vision-enabled Qwen profile; this compares individual
models rather than a panel mixing them with paid-provider fallback.

The database transaction is repeatable-read and **read-only**, then rolled back
and closed before model/search requests. Production decisions, accepted products,
collaboration tables and persistent search caches are not written. Existing
accepted-product search priors are excluded to avoid leaking an already-known
identity into model discovery. Source citations and arithmetic passes are never
invented from evaluation labels.

`RECEIPT_ROOT` defaults to `.receipt-test/inbox`, allowing your local copy of the
Sobeys PDF to be used without mounting the archive. It must match the source hash
recorded in the database. For other receipts, point it at the source archive:

```sh
make eval-enrichment RECEIPT_ROOT=/Volumes/files/brownrook/home-budget/receipts/raw/scanned/inbox
```

Both models use the production image renderer and its page selection/bounds.
Unreadable/missing/hash-mismatched source images make the result incomplete. OCR
artifact errors and missing arithmetic checks retain the production conservative
review behavior. Fix missing evidence rather than manufacturing a passing check.

## Same retailer evidence, independent model decisions

Identical retailer queries reuse one captured raw response (including failures)
within the case. Each model still starts with an independent candidate pool and
must issue its own queries to access those results. One model's proposals or
unverified candidates are not passed to the other model. This reduces repeat
search charges and holds initial results fixed. Live searches may change between
separate experiments. The command uses `BRAVE_SEARCH_API_KEY`; exported variables
override `.env.dev`. Clear a stale exported key with
`unset BRAVE_SEARCH_API_KEY` to use the file's key.

The safe launcher reads relevant database, storage, Brave and Ollama settings
without executing the environment file. Use `EVAL_ENV_FILE` for another ignored
file. Credentials and database connection strings are not written into reports.
Both models must already be installed in the selected Ollama service.

## Informational reports

Private, Git-ignored reports remain under `.model-evals/<run-id>/`. The report
retains real input evidence, image hashes, OCR alternatives, descriptor/brand
expansions and attempts, candidates and scored support, source citations, reviews,
decision reasons, model stage/GPU timings and captured retailer responses.
References are used only after the model's decision, never in model prompts.

Quality disagreements and conservative review decisions are informational. Model,
search or image failures are **incomplete**, not model losses. They return success
so this experiment does not become a required CI quality gate. Invalid CLI/case
configuration or inability to write a report remains a program error. No live
comparison is installed as a required CI job. Software unit tests still catch
implementation regressions.

The report's category remains the application's built-in deterministic category
on a recommended product label. Database category overrides and AI category
fallback are not exercised. This is stated in each qualification result; it does
not claim that the entire application's categorization workflow ran. Confidence
remains a lexical evidence heuristic, not a correctness probability. Review
product URL, title, package variant, evidence citations and decision reasons
before promoting a model through GitOps.

## Older experiment

The previous paired query-expansion implementation remains available explicitly:

```sh
PYTHONPATH=src .venv/bin/python scripts/eval_enrichment.py --env-file .env.dev \
  --workflow paired --baseline qwen3-vl-receipts:30b-instruct --candidate qwen3.8:27b
```

`make eval-enrichment` defaults to the production collaboration path. No deployment
or merge is needed to test an unmerged branch locally.
