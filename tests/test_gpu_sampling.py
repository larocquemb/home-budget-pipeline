import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

from home_budget_pipeline.gpu_sampling import GpuSampler


def test_unobserved_gpu_time_is_not_reported_as_idle():
    sampler = GpuSampler(lambda *a: None, {})
    sampler.interval = 1
    sampler.elapsed = 5
    sampler.samples = [(0, 80), (1, 0), (4, 40)]
    sampler.errors = 2
    summary = sampler.summary()
    assert summary['gpu_sampled_seconds'] == 3
    assert summary['gpu_util_avg_percent'] == 40
    assert summary['gpu_util_max_percent'] == 80
    assert summary['gpu_sampled_active_seconds'] == 2
    assert summary['gpu_sampling_errors'] == 2
    sampler.samples = []
    assert sampler.summary()['gpu_util_avg_percent'] is None
    assert sampler.summary()['gpu_sampled_active_seconds'] is None


def test_gpu_samples_are_correlated_and_endpoint_failure_is_observable(monkeypatch):
    import home_budget_pipeline.gpu_sampling as sampling
    events = []
    context = {'receipt': 'a.pdf', 'item_id': 4, 'run_uuid': 'test-run'}
    sampler = GpuSampler(lambda event, data: events.append((event, data)), context)
    sampler.started = 0
    monkeypatch.setattr(sampling.time, 'monotonic', lambda: .5)
    monkeypatch.setattr(sampling.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(json.dumps({
        'timestamp': '2026-10-04T15:00:00Z', 'gpu_host': 'arsene',
        'gpus': [{'gpu_index': 0, 'gpu_util_percent': 67, 'gpu_memory_bytes': 1000}],
    }).encode()))
    monkeypatch.setattr(sampler.stop, 'wait', lambda interval: sampler.stop.set())
    sampler.poll()
    assert events[0][0] == 'enrichment_gpu_sample'
    assert all(events[0][1][key] == value for key, value in context.items())
    assert events[0][1]['gpu_util_percent'] == 67
    assert sampler.samples == [(.5, 67)]
    sampler.stop.clear()
    def fail(*a, **kw):
        raise OSError('private endpoint unreachable')
    monkeypatch.setattr(sampling.urllib.request, 'urlopen', fail)
    sampler.poll()
    assert sampler.errors == 1 and len(events) == 1


def test_host_gpu_reader_handles_unavailable_metrics(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'ops/ollama/roles/ollama/files/gpu_telemetry.py'
    spec = importlib.util.spec_from_file_location('gpu_telemetry', path)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout='0, GPU-abc, 67, 21053, 139, 44\n1, GPU-def, [N/A], [N/A], [N/A], [N/A]\n')
    monkeypatch.setattr(host.subprocess, 'run', run)
    first, second = host.read_gpus()
    assert first['gpu_util_percent'] == 67
    assert first['gpu_memory_bytes'] == 21053 * 1024 ** 2
    assert second['gpu_util_percent'] is None and second['gpu_memory_bytes'] is None
    assert calls[0][0][0] == 'nvidia-smi' and calls[0][1]['timeout'] == 2
