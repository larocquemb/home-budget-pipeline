# Arsene Ollama monitoring

Ollama stays a native systemd service on Arsene. Native Grafana Alloy forwards
`ollama.service` and `ollama-development.service` journal entries to the existing Loki server over mTLS.
No Kubernetes collector, ingress, or application deployment is needed for this
monitoring path. Local `sudo journalctl -u ollama.service` remains available.

The existing helper at `192.168.2.201:11435` retains `/gpu` for application
sampling and adds `/metrics` for continuous Prometheus scraping. The monitoring
host's existing Alloy scrapes it every 15 seconds and forwards measurements to
its loopback Prometheus receiver. Metrics expose resource information and
loaded model names; neither endpoint exposes prompts, responses, or model
management. Existing private-network access restrictions apply to port 11435.

## Measurements

- Host CPU, available RAM, and swap come from `/proc`.
- Managed Ollama CPU, RAM, and swap come from its cgroup v2. This includes all
  model-runner child processes, not just the idle API parent process.
- Service RAM is `memory.current`, including charged file cache. It can remain
  high after unloading; host available RAM shows reclaimable headroom. This is
  not summed process RSS or GPU VRAM.
- CPU is measured as a counter; its rate is CPU cores used (1.0 is one core).
- NVIDIA utilization, VRAM, power and temperature include all host GPU work,
  including Jupyter. They are not exclusive per-model accounting.
- `/api/ps` supplies loaded model allocation and context length. Polling it
  does not load a model. Empty inventory means no loaded models.
- Collector health reports unavailable sources; failures do not become idle
  zeroes. The endpoint can return healthy host metrics while NVIDIA is failing.

The **Arsene Ollama** dashboard in Infrastructure combines these charts with
service logs and links to Home Budget model comparisons. Token throughput,
generation time, and per-receipt request measurements remain in the existing
application comparison dashboard. The helper is not an inference proxy and
cannot measure token throughput for arbitrary clients.

## Review and apply

The operations commands run on the Mac. Use the existing private
`.env.monitoring` and Brown Rook PKI identity. The Ollama wrapper now loads that
file to obtain `MONITORING_PKI_DIR`. It defaults to the existing trusted
`alloy-loki-client` identity; source paths can be overridden in inventory if a
separate client certificate is issued for Arsene. Certificate keys never enter
Git and are installed root:alloy mode 0640.

```bash
make ollama-gitops-syntax monitoring-gitops-syntax
make ollama-gitops-check
make ollama-gitops-apply
make monitoring-gitops-check
make monitoring-gitops-apply
```

Check/apply prompts for the target host's sudo password. Monitoring apply also
uses the existing Grafana credentials. Ollama apply installs pinned native
Alloy from Grafana's signed RPM repository, enables journal access and service
accounting, and keeps request debug logging disabled. Its first apply restarts
Ollama and the helper: finish active inference jobs first. Later reconciliation
restarts only services whose configuration changed. The collector's own HTTP
administration listener is loopback-only. Check mode on a host without Alloy
reports the installation but skips tasks that require its user and binary.

The service log stream is labelled `host=arsene`, `service=ollama`,
`collection=host-journal`, `environment=production`. It covers the managed
services only, not Paul's separate terminal `ollama serve`. Startup environment
dumps are dropped and credential patterns redacted. The journal reader starts
with up to one hour of retained history and persists its cursor through restarts;
Loki retention remains governed by the existing monitoring configuration.

## Verify

From the Mac:

```bash
curl --fail http://192.168.2.201:11435/metrics
curl --fail http://192.168.2.201:11435/gpu
```

Check `ollama_service_collector_success`, `ollama_host_collector_success`,
`ollama_gpu_collector_success`, and `ollama_api_up` are 1. On Arsene:

```bash
systemctl is-active ollama ollama-gpu-telemetry alloy
sudo journalctl -u alloy -n 50 --no-pager
sudo journalctl -u ollama.service -n 50 --no-pager
```

In Grafana Explore, query Prometheus:

```promql
up{job="ollama-host"}
rate(ollama_service_cpu_seconds_total{host="arsene"}[5m])
ollama_service_memory_bytes{host="arsene"}
```

Query Loki:

```logql
{host="arsene", service="ollama", collection="host-journal"}
```

Run an ordinary request through `http://192.168.2.201:11434`, then verify new
server events and GPU activity in the dashboard. A running collector alone is
not proof of journal permissions, certificate acceptance, or log delivery.

## Recovery

Revert the monitoring configuration in Git and reapply. Set
`ollama_monitoring_enabled=false` to skip management of the native journal agent;
this does not stop an already-installed agent. To stop forwarding immediately,
run `sudo systemctl disable --now alloy` on Arsene. The existing `/gpu` endpoint
and local Ollama journal remain independent of forwarding.


## Shared GPU Ollama service

The service on port 11434 handles production and development requests. The
helper reports a single service and its loaded models; it cannot attribute
resource usage separately to production and development clients. Logs use
`service="ollama"` and `environment="shared"`. GPU metrics include all
workloads on the host, including Jupyter.
