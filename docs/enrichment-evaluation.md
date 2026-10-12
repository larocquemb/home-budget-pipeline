# Informational product enrichment evaluation

Use the existing paired enrichment experiment to compare two Ollama models after
OCR. Both receive the same receipt item, merchant, top baseline retailer results,
query-expansion prompt and schema. Each model's proposed queries are searched and
scored against the **original receipt text**. A model cannot borrow the other
model's newly discovered candidates. Identical queries reuse in-memory results
within the item. The command does not connect to PostgreSQL or write accepted
products, decisions, persistent search caches or production configuration.

```sh
# Reads selected pipeline settings from .env.dev; exported variables take precedence.
OLLAMA_BASE_URL=http://192.168.2.201:11434 make eval-enrichment \
  BASELINE=qwen3-vl-receipts:30b-instruct CANDIDATE=qwen3.8:27b
```

Both models must be installed in the managed LAN service. The launcher safely
parses the existing `.env.dev` without executing it and loads only the Brave and
Ollama settings it needs. Exported variables take precedence; use
`EVAL_ENV_FILE=/path/to/ignored/env-file` for another configuration. It never
saves credentials. Retailer
searches can incur Brave API usage. Run this manual experiment when Arsene is
idle, as models share its GPU with production. The default manifest contains the
printed `Cep Pic Med` and prior OCR `Oep Pic Med` spellings. The expected product
URL/name comes from the existing Sobeys regression fixture, with Groceries as
the reference category. This reference is an existing product expectation, not
independent proof of package size or purchase identity.

Change `ENRICHMENT_EVAL_CASES=/path/to/cases.json` to add receipt items and optional
`expected_product_url`, `expected_product_name` and `expected_category`. URL and
category agreement are explicitly scored; the expected name is retained for
review rather than fuzzy scoring. Case IDs must be unique and merchants must be
supported by the existing retailer search code. Raw receipt inputs, not corrected
model outputs, should be the same for both models.

Reports are saved privately under `.model-evals/<run-id>/report.md` and
`report.json`, already ignored by Git. They include selected product labels,
retailer links/snippets, proposed queries, baseline evidence, candidate origin,
confidence gain, acceptance by the existing lexical scorer, reference agreement,
model digests, exact shared prompts, settings, token timings and all retrieved
query results. Labels can contain receipt data: do not commit private reports.

Quality disagreements, no matches and API errors are informational. API failures
are labelled **incomplete**, with null reference agreement; they never count as
model losses. Missing credentials or unavailable models likewise generate an
incomplete report. These conditions return exit zero, so this manual evaluation
can also be invoked by an optional CI job without blocking a build. Malformed
configuration, invalid CLI arguments and report-writing errors remain program
errors. No live model evaluation has been added to required CI jobs. Normal unit
tests continue to catch software regressions.

## Scope and interpretation

This reuses the actual `product_comparison.baseline_search`, `evaluate`, query
retry policy, retailer search client and evidence scoring functions. It is the
existing paired product-enrichment experiment, **not** the entire production
collaboration workflow with image evidence, voting, product verification,
Brave cache database, application-specific category overrides or AI fallback.

Categories use the application's built-in deterministic category rules on an
accepted product's cleaned label. The report states that scope explicitly; it
cannot claim to have validated database category mappings. A model that returns
no acceptable product has no category prediction. Category differences should
be inspected separately from product identity.

Confidence is a lexical evidence heuristic, not an accuracy probability. Models
may agree because the baseline retailer search already found the correct product;
check `candidate_origin` and `confidence_gain` before attributing the match to
model reasoning. Each model can add its own searches, so final candidate pools
can differ even though initial evidence is identical. Preserve these reports
when comparing subsequent prompt or model changes: live retailer results can
change between experiments. Product-page matching alone does not verify package
size, variant or actual purchase identity. Review those details before promoting
any model through GitOps.
