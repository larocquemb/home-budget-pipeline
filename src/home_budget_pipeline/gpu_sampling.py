"""Sample host GPU activity during a model request, without equating memory to work."""
import json
import os
import threading
import time
import urllib.request


class GpuSampler:
    def __init__(self, emit, context):
        self.emit, self.context = emit, context
        self.interval = max(.2, float(os.getenv("GPU_SAMPLE_INTERVAL_SECONDS", "1")))
        self.url = os.getenv("OLLAMA_GPU_METRICS_URL", "http://192.168.2.201:11435/gpu")
        self.stop = threading.Event()
        self.samples = []
        self.errors = 0
        self.identity = {}
        self.elapsed = 0

    def poll(self):
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=2) as response:
                    payload = json.load(response)
                gpu = next(g for g in payload["gpus"]
                           if g["gpu_index"] == int(os.getenv("OLLAMA_GPU_INDEX", "0")))
                if not isinstance(gpu.get("gpu_util_percent"), (int, float)):
                    raise ValueError("GPU utilization unavailable")
                elapsed = time.monotonic() - self.started
                if self.stop.is_set():
                    break
                self.samples.append((elapsed, gpu["gpu_util_percent"]))
                self.identity = {"gpu_host": payload["gpu_host"],
                                 "gpu_uuid": gpu.get("gpu_uuid"), "gpu_index": gpu["gpu_index"]}
                self.emit("enrichment_gpu_sample", {**self.context, **gpu,
                          "gpu_host": payload["gpu_host"], "sample_timestamp": payload["timestamp"],
                          "elapsed_seconds": round(elapsed, 3)})
            except Exception:
                self.errors += 1
            self.stop.wait(self.interval)

    def __enter__(self):
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.poll, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.elapsed = time.monotonic() - self.started
        self.stop.set()
        self.thread.join(timeout=3)

    def summary(self):
        # Treat each observation as valid for at most one configured interval.
        # Gaps and absent samples are unobserved, never silently zero utilization.
        weights = [max(0, min(self.interval, self.elapsed - offset,
                             self.samples[index + 1][0] - offset if index + 1 < len(self.samples) else self.interval))
                   for index, (offset, _) in enumerate(self.samples)]
        covered = sum(weights)
        return {**self.identity, "gpu_sample_count": len(self.samples), "gpu_sampling_errors": self.errors,
                "gpu_sampled_seconds": round(covered, 3),
                "gpu_util_avg_percent": round(sum(w * p for w, (_, p) in zip(weights, self.samples)) / covered, 2) if covered else None,
                "gpu_util_max_percent": max((p for _, p in self.samples), default=None),
                "gpu_sampled_active_seconds": round(sum(w for w, (_, p) in zip(weights, self.samples) if p > 0), 3) if covered else None}
