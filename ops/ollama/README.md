# Arsene shared Ollama service

Ansible manages one GPU-backed `ollama.service` listening on port 11434, the
standard Ollama port. It binds IPv4 interfaces so local clients can use the
Ollama default and private LAN/K3s clients can reach Arsene. The dedicated
nftables table restricts port 11434 and telemetry port 11435 to loopback and
approved private source networks. Do not remove or bypass this firewall.

Model files remain in `/home/ollama/models`. The service allows one loaded
model and one parallel request. Production and development share the runner;
requests may queue. Jupyter and independent CUDA programs also share GPU memory.

The legacy CPU service and `ollama-development.service` are retired. Do not
apply the old split-service role after migration. Grafana Alloy and the GPU
telemetry helper remain managed; metrics represent one shared service rather
than separate production and development services.

Migration requires coordinating all existing clients from 11436 to 11434, and
stopping the old CPU and GPU units before enabling the single shared service.
Apply Ansible only after reviewing the cutover and communicating its brief
inference interruption. Check both `http://127.0.0.1:11434/api/tags` locally and
`http://192.168.2.201:11434/api/tags` from authorized clients. Verify production
worker and Jupyter endpoints and the telemetry metrics before declaring success.
