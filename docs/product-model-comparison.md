# Compare OpenAI and Qwen product matches

Use `make enrich-products COMPARE=1` to run both models on the **same database
receipt items**. This is product enrichment after OCR, not a comparison of OCR
engines. It calls OpenAI and Arsene's Ollama service on every selected item,
including items already enriched. It does not wait for the normal AI fallback.

Both models receive identical prompts and JSON schemas and may propose at most
three searches. Brave retrieves retailer evidence. The existing match scorer
evaluates that evidence against the **original item text**, not the model's
expanded words. Identical queries share their search response within an item.
Confidence is a token/barcode matching heuristic, **not a calibrated probability
of correctness**. Abbreviated OCR text can score poorly even for a correct
expansion. Review the URLs, titles and snippets alongside the scores. Equal
scores can refer to different products.

The job records append-only pairs in `enrichment.product_comparisons` and emits
structured logs for Grafana. It does not change receipt items, accepted product
cache entries or enrichment audit records. Provider/search failures are recorded
with null confidence and an incomplete pair; they do not count as model wins.
The job finishes with a nonzero exit status if any pairs were incomplete.

## Deploy the comparison mode

Merge and deploy the application changes through the normal GitOps release
workflow. The existing database PreSync upgrade creates the comparison table;
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

## Make Ollama reachable

On Arsene, inspect the listener and installed model:

```sh
ollama list
ss -ltnp | grep 11434
```

The job must reach the service from Kubernetes. If Ollama only listens on
localhost, bind it to Arsene's LAN address using its systemd override:

```sh
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=192.168.2.201:11434"\n' |
  sudo tee /etc/systemd/system/ollama.service.d/listen.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Allow TCP 11434 from the cluster through the host's managed firewall if needed;
keep this unauthenticated Ollama endpoint on the private network. Check
reachability from your Mac before running the job:

```sh
curl --fail --max-time 5 http://192.168.2.201:11434/api/tags
```

Qwen loads automatically on the first request and stays loaded for five minutes
after requests. Model calls time out after 180 seconds, including cold loading.
The job runs providers/items sequentially and uses one Ollama model instance.
The caller Pod needs no GPU allocation; Ollama on Arsene owns the RTX 3090.
Check actual offload on Arsene during the experiment:

```sh
ollama ps
watch -n 1 nvidia-smi
```

The dashboard's GPU model memory panel comes from Ollama `/api/ps` `size_vram`;
it is observed allocation, not GPU utilization or proof of a speedup.

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
item uses two model calls and up to six Brave searches. OpenAI and Brave usage
can incur charges. Run one experiment at a time to keep GPU and service load
bounded. The Make target follows logs and reports completion/failure; Ctrl+C
stops log following but leaves this finite Job running.

The dashboard shows evidence scores, accepted matches at the 0.85 threshold,
acceptance rates, score wins/ties, exact product URL agreement, model response
times (including cold load), observed Qwen GPU memory, errors and per-item paired
evidence. Failed providers are excluded from score and acceptance summaries.
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
