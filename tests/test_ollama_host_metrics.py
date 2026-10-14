"""Host telemetry must include runner cgroups and distinguish failures from idle."""
import importlib.util
from pathlib import Path
import subprocess


def collector():
    path = Path(__file__).parents[1] / 'ops/ollama/roles/ollama/files/gpu_telemetry.py'
    spec = importlib.util.spec_from_file_location('ollama_host_metrics', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cgroup_cpu_and_memory_are_service_wide(tmp_path):
    (tmp_path / 'cpu.stat').write_text('usage_usec 2500000\nuser_usec 2000000\n')
    (tmp_path / 'memory.current').write_text('123456789\n')
    (tmp_path / 'memory.swap.current').write_text('1024\n')
    assert collector().read_service(tmp_path) == {
        'cpu_seconds_total': 2.5, 'memory_bytes': 123456789, 'swap_bytes': 1024}


def test_host_cpu_does_not_double_count_guest_time(tmp_path):
    (tmp_path / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 200 kB\nSwapTotal: 50 kB\nSwapFree: 40 kB\n')
    (tmp_path / 'stat').write_text('cpu  10 20 30 40 50 60 70 80 90 100\n')
    memory, cpu = collector().read_host(tmp_path)
    assert memory['MemAvailable'] == 200 * 1024
    assert cpu == [10, 20, 30, 40, 50, 60, 70, 80]


def test_failed_collectors_do_not_claim_idle_zeroes(tmp_path, monkeypatch):
    module = collector()
    def fail(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(module, 'read_host', fail)
    monkeypatch.setattr(module, 'read_gpus', fail)
    monkeypatch.setattr(module, 'read_models', fail)
    metrics = module.prometheus_metrics(tmp_path, 'http://invalid')
    for source in ('host', 'gpu', 'service'):
        assert f'ollama_{source}_collector_success 0' in metrics
    assert 'ollama_api_up 0' in metrics
    assert 'ollama_loaded_models ' not in metrics
    assert 'ollama_service_memory_bytes ' not in metrics


def test_multiple_gpu_series_have_one_type_declaration(tmp_path, monkeypatch):
    module = collector()
    monkeypatch.setattr(module, 'read_host', lambda: ({'MemTotal': 100, 'MemAvailable': 50,
        'SwapTotal': 0, 'SwapFree': 0}, [100, 0, 100, 1000, 0, 0, 0, 0]))
    monkeypatch.setattr(module, 'read_service', lambda _: {'cpu_seconds_total': 2,
        'memory_bytes': 1024, 'swap_bytes': 0})
    gpu = {'gpu_util_percent': 80, 'gpu_memory_bytes': 1000, 'gpu_memory_total_bytes': 2000,
           'gpu_power_watts': 200, 'gpu_temperature_c': 70}
    monkeypatch.setattr(module, 'read_gpus', lambda: [{**gpu, 'gpu_index': 0}, {**gpu, 'gpu_index': 1}])
    monkeypatch.setattr(module, 'read_models', lambda _: [{'name': 'qwen:30b',
        'size': 2000, 'size_vram': 1800, 'context_length': 4096}])
    metrics = module.prometheus_metrics(tmp_path, 'http://unused')
    assert metrics.count('# TYPE ollama_gpu_util_percent gauge') == 1
    assert 'ollama_gpu_util_percent{gpu_index="1"} 80' in metrics
    assert 'ollama_model_size_vram{model="qwen:30b"} 1800' in metrics
    assert 'ollama_api_up 1' in metrics


def test_gpu_failure_preserves_service_metrics(tmp_path, monkeypatch):
    module = collector()
    monkeypatch.setattr(module, 'read_service', lambda _: {'cpu_seconds_total': 1,
        'memory_bytes': 1024, 'swap_bytes': 0})
    monkeypatch.setattr(module, 'read_models', lambda _: [])
    def fail():
        raise subprocess.TimeoutExpired('nvidia-smi', 2)
    monkeypatch.setattr(module, 'read_gpus', fail)
    metrics = module.prometheus_metrics(tmp_path, 'http://unused')
    assert 'ollama_gpu_collector_success 0' in metrics
    assert 'ollama_service_memory_bytes 1024' in metrics
    assert 'ollama_loaded_models 0' in metrics


def test_split_service_metrics_do_not_confuse_gpu_and_cpu(tmp_path, monkeypatch):
    module = collector()
    production = tmp_path / 'production'
    development = tmp_path / 'development'
    def service(path):
        if path == production:
            raise OSError('production stopped')
        return {'cpu_seconds_total': 2, 'memory_bytes': 1024, 'swap_bytes': 0}
    monkeypatch.setattr(module, 'read_service', service)
    monkeypatch.setattr(module, 'read_gpus', lambda: [])
    def models(url):
        if url.endswith(':11434'):
            raise OSError('production unreachable')
        return [{'name': 'qwen', 'size': 100, 'size_vram': 80}]
    monkeypatch.setattr(module, 'read_models', models)
    metrics = module.prometheus_metrics(production, 'http://host:11434', development, 'http://host:11436')
    assert 'ollama_api_up{workload="production"} 0' in metrics
    assert 'ollama_api_up{workload="development"} 1' in metrics
    assert 'ollama_service_collector_success{workload="production"} 0' in metrics
    assert 'ollama_service_memory_bytes{workload="development"} 1024' in metrics
    assert 'ollama_model_size_vram{workload="development",model="qwen"} 80' in metrics
    assert metrics.count('# TYPE ollama_api_up gauge') == 1
