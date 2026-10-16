# Shared RTX 3090 inference (KAN-135)

Receipt inference, enrichment, local development, and Jupyter use the existing
GPU service at `http://192.168.2.201:11436`. Kubernetes callers and application
fallback URLs use this endpoint. The CPU service on 11434 remains available
for explicit diagnostics; its systemd isolation is unchanged.

The shared GPU service allows one loaded model and one parallel Ollama request.
There is no GPU reservation mechanism: direct Jupyter CUDA allocations can still
compete with Ollama for VRAM. Avoid running those workloads simultaneously.
The 15-minute receipt discovery schedule is unchanged; product enrichment remains
an on-demand, suspended CronJob template. Existing Jobs keep their original
configuration and must finish before evaluating the new routing.

Production clients use server thread defaults and retain a 300-second Ollama
request timeout. Cloud provider waits remain 180 seconds. The one-hour timeout
was a diagnostic setting only.

The matched saved review used 8,795 input tokens. GPU completed in 15.13 seconds
(7.34 seconds prompt processing, 2.13 seconds generating 194 output tokens).
CPU with five threads and raised memory limits completed in 294.53 seconds
(261.77 seconds prompt processing, 31.85 seconds generating 171 tokens).
The CPU model was already loaded; these are not matched cold-start measurements.
This result motivates GPU routing but is not whole-pipeline validation.

The existing model store `/home/ollama/models` is preserved and shared.
The development service owns model download/create/update operations;
production mounts the model directory read-only. Automatic pruning is disabled
in both services. Do not delete or replace model blobs while either service uses
them. Changing the receipt model remains a separate reviewed change.

The dedicated `ollama_access` nftables table permits LAN `192.168.2.0/24`,
VPN `10.6.0.0/24`, K3s pod network `10.42.0.0/16`, and loopback on the three
ports; other sources are rejected. It preserves K3s and Jupyter firewall tables.
The API is unauthenticated and stays private. Existing LAN/VPN trust is required.

## Rollout

1. Review and merge the pipeline PR. From the checked-out main branch:

   ```sh
   make ollama-gitops-syntax
   make ollama-gitops-check
   ```

2. Finish active inference before applying: production restarts to unload GPU
   runners and enforce CPU-only execution. Saved models and receipt data are
   retained. Apply from the LAN/VPN controller using the existing monitoring
   credentials and PKI configuration:

   ```sh
   make ollama-gitops-apply
   make monitoring-gitops-check
   make monitoring-gitops-apply
   ```

3. Update each development checkout's ignored `.env.dev`, preserving all other
   credentials and settings:

   ```sh
   make ollama-dev-config
   set -a
   source .env.dev
   set +a
   ollama list
   ```

   Restart existing local worker processes after sourcing the new settings.
   Existing shells and processes may retain the old exported URL. `.env.dev.example`
   supplies the new GPU URL for fresh checkouts. Production caller settings stay
   unchanged. On Arsene use `OLLAMA_HOST=http://192.168.2.201:11436 ollama pull MODEL`
   for explicit model installation; the playbook does not download weights.

4. Review and merge the infrastructure PR, issue the Brownrook CA Jupyter leaf,
   and run its documented Jupyter Ansible playbook. It changes Jupyter's Ollama
   URL to 11436 and adds the internal HTTPS alias, retaining the public route.
   Save notebooks before applying; restart existing kernels afterward.

## Verify hardware placement and actual receipt latency

From a client, `/api/tags` checks both services without loading a model:

```sh
curl --fail http://192.168.2.201:11434/api/tags
curl --fail http://192.168.2.201:11436/api/tags
```

Run a bounded comparison using a receipt PNG/JPEG (render PDFs first):

```sh
.venv/bin/python scripts/benchmark_ollama_split.py \
  --image /path/to/sobeys.png --repeats 2 --concurrent
```

The default receipt vision model is `qwen3-vl-receipts:30b-instruct`.
Image-mode requests use five CPU threads (`--num-threads` overrides this),
matching the deployed production callers. JSON replay preserves the recorded
request settings, including its thread count.
For production-equivalent prompt, model, and context/output settings, replay a
saved Ollama `/api/chat` request instead:

```sh
.venv/bin/python scripts/benchmark_ollama_split.py \
  --request-json /private/path/receipt-request.json --repeats 2 --concurrent
```

The report saves private model responses and loaded-model allocations under
`.model-evals/`; never commit or publish receipt data. It requires CPU placement
on production and GPU placement on development. It measures the inference call,
not the complete receipt job. Review cold load and warm repeats separately.
Run the actual receipt job afterward and inspect its existing queue/retry history,
whole-job duration, and structured application measurements.

**The benchmark's 900-second HTTP timeout is measurement-only.** Production
Qwen workers and enrichment jobs set `OLLAMA_TIMEOUT_SECONDS=300`; other
providers retain `COMPARISON_TIMEOUT_SECONDS=180`. The Ollama adapters prefer
the dedicated setting and otherwise use the existing comparison timeout.
These are HTTP socket timeouts, not a total receipt-job deadline. Verify actual
production requests and whole jobs after rollout; the image benchmark is not
a complete receipt or enrichment test. Do not infer vision-model performance
from an unrelated 27B text-generation benchmark. Financial/model-quality
results remain informational; there is no new CI accuracy gate.

On Arsene verify isolation directly:

```sh
systemctl is-active ollama ollama-development ollama-gpu-telemetry alloy
systemctl show ollama -p PrivateDevices -p CPUAffinity -p CPUQuotaPerSecUSec -p MemoryMax
OLLAMA_HOST=http://192.168.2.201:11434 ollama ps
OLLAMA_HOST=http://192.168.2.201:11436 ollama ps
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
sudo journalctl -u ollama -u ollama-development -n 100 --no-pager
```

Confirm production runners never appear in NVIDIA's process inventory while
CPU inference is active. Check cgroup membership of GPU PIDs if needed. The
benchmark's placement check supplements this; it does not prove all processes
on the host have no GPU access. Jupyter CUDA smoke tests are in brownrook-infra.

## Monitoring

The helper emits service and model metrics with `workload="production"` or
`workload="development"`. The existing monitoring scrape remains on port 11435.
Host CPU/RAM and NVIDIA measurements remain host-wide, including Jupyter.
The Arsene Ollama Grafana dashboard distinguishes both service/model series.

```promql
rate(ollama_service_cpu_seconds_total{host="arsene",workload="production"}[5m])
ollama_service_memory_bytes{host="arsene",workload="development"}
ollama_model_size_vram{host="arsene",workload="production"}
ollama_api_up{host="arsene"}
```

Loki collects both native services with distinct environment labels:

```logql
{host="arsene",service="ollama",environment="production"}
{host="arsene",service="ollama-development",environment="development"}
```

Logs include server request events, failures, and request timing where emitted
by Ollama; the helper does not synthesize token throughput or request histograms.
Use the benchmark/application comparison reports for latency and throughput,
and existing RabbitMQ monitoring for backlog. Local journals remain available.

## Rollback and recovery

Revert the native service changes in Git, review/merge, and reapply to restore
production GPU execution on its unchanged 11434 endpoint. Explicitly stop and
disable `ollama-development` before that rollback so its GPU runner cannot
compete with production. A reverted role does not automatically remove a unit
created by an earlier revision. Keep 11436 unused until development resumes.
Revert Jupyter's endpoint and the client `.env.dev` URLs to 11434 at the same time.
Retain model files and data. For an emergency, stop the development service,
restore the previously reviewed `listen.conf`, and reconcile the corresponding
Git rollback promptly; do not leave a persistent manual configuration fork.

Notebook CUDA experiments still share the 3090 with development Ollama. Unload
idle development models before a large PyTorch experiment. CPU production also
uses shared host RAM, CPU, and storage, so monitor concurrent pressure.

## Production CPU thread tuning

Production Qwen workers and enrichment jobs set `OLLAMA_NUM_THREADS=5`. Both
Ollama request adapters translate it to `options.num_thread`; unset or blank
leaves server defaults intact for development GPU clients. Invalid nonpositive
or noninteger values fail before inference. No model parameter or service
environment variable is changed by this request setting.

On Arsene, a controlled 37-output-token prompt measured warm generation at
2.28, 4.33, 5.82, 6.70, 7.04, and 6.85 tokens/second for one through six threads.
The integrated Radeon achieved 1.37 tokens/second at a matched 4096-token
context. Production retains CPU-only device isolation, its six-core quota, and
port 11434; development retains the RTX 3090 on 11436. These short-prompt
measurements do not establish complete receipt latency or replace the
cold-image measurements below. Validate a real receipt after rollout.

Deploy the rebuilt application image through the existing GitOps workflow.
The changed Qwen Deployment restarts workers; new enrichment Jobs inherit
five threads. Existing Jobs and local processes need restarting/updating.
Jupyter and the development configuration do not set this variable.

## Historical CPU timeout follow-up (KAN-135)

On October 16, 2026, after rebooting Arsene with the Git-managed six-core quota,
CPU affinity 0-7, and GPU device isolation, a five-thread receipt-image request
using `qwen3-vl-receipts:30b-instruct` completed in 193.05 seconds on production:
9.07 seconds loading, 178.86 seconds prompt/image processing, and 5.05 seconds
generating. Development completed the same request in 4.74 seconds on GPU.
The subsequent concurrent requests took 5.51 seconds CPU and 0.68 seconds GPU;
those repeated-image timings benefit from prompt caching and must not be used
as estimates for a new receipt image. All four placement checks passed with
the same model digest. The request used context 4096 and at most 256 output
tokens; production collaboration budgets and its default model differ.

The 300-second production Ollama timeout gives this measured cold request
approximately 107 seconds of headroom. It is an initial measured setting, not
proof that all models, images, output budgets, queue delays, or complete jobs
fit. Keep KAN-135 open until representative production jobs and retry behavior
are verified under the deployed configuration.

No transport retries are added: a timed-out model request may still be running
on the server. Existing empty-output, truncation, and citation-correction retries
remain bounded and distinct from transport errors. RabbitMQ task IDs, advisory
locks, persisted results, and ACK-after-commit behavior are unchanged; they do
not guarantee exactly-once inference following process interruption. The
existing 900-second collaboration round timeout, worker termination grace, and
broker acknowledgement timeout are unchanged and still need whole-job testing.

Deploy the updated application image and manifests through the existing
GitOps workflow. The Qwen Deployment rolls its worker; new enrichment Jobs
inherit the setting, while existing Jobs must finish or be handled explicitly.
Production remains on 11434 and development on 11436. To roll back, revert the
Ollama timeout override in Git and reconcile; other provider waits and receipt
data are unaffected. Private benchmark reports stay under `.model-evals/` and
must not be committed. The report-writer fix uses built-in `open` with mode
0600; the regression test exercises actual report creation.

## Deploy shared GPU routing

Deploy through `make deploy-k3s` using the existing GitOps workflow. Verify the
Qwen Deployment rolls out with `OLLAMA_BASE_URL=http://192.168.2.201:11436` and
without `OLLAMA_NUM_THREADS`. New enrichment Jobs inherit the same endpoint.
Run a bounded real receipt job and verify its outcome and GPU placement before
closing KAN-135. Earlier CPU routing and tuning sections below the introduction
record the previous deployment; the shared GPU routing above supersedes them.

To roll back, revert this routing change in Git and reconcile. The CPU service
on 11434 remains available, but representative image reviews can approach or
exceed the retained 300-second timeout. Receipt data and model files are unchanged.
