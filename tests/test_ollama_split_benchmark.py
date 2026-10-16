"""A benchmark must not report a missing or GPU-backed production model as CPU-only."""
import importlib.util
import json
from pathlib import Path
import stat
import sys

import pytest


@pytest.mark.parametrize('replay', [False, True])
def test_benchmark_writes_private_report_with_production_threads(monkeypatch, tmp_path, replay):
    path = Path(__file__).parents[1] / 'scripts/benchmark_ollama_split.py'
    spec = importlib.util.spec_from_file_location('benchmark_ollama_split', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / 'private' / 'report.json'
    args = ['benchmark', '--repeats', '1', '--concurrent', '--output', str(output)]
    if replay:
        source = tmp_path / 'request.json'
        source.write_text(json.dumps({'model': 'test', 'messages': [], 'options': {'num_thread': 3}}))
        args += ['--request-json', str(source)]
    else:
        source = tmp_path / 'receipt.png'
        source.write_bytes(b'\x89PNG-test')
        args += ['--image', str(source)]
    bodies = []
    def measure(base, workload, body, timeout):
        bodies.append(body)
        return {'workload': workload, 'wall_seconds': 1, 'placement_pass': True, 'completed': True}
    monkeypatch.setattr(module, 'measure', measure)
    monkeypatch.setattr(sys, 'argv', args)
    module.main()
    report = json.loads(output.read_text())
    assert len(report['runs']) == len(bodies) == 4
    assert all(body['options']['num_thread'] == (3 if replay else 5) for body in bodies)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


def test_hardware_placement_requires_the_requested_loaded_model():
    path = Path(__file__).parents[1] / 'scripts/benchmark_ollama_split.py'
    spec = importlib.util.spec_from_file_location('benchmark_ollama_split', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not module.placement([], 'qwen', 'production')
    assert not module.placement([{'name': 'other', 'size_vram': 0}], 'qwen', 'production')
    assert not module.placement([{'name': 'qwen', 'size_vram': 123}], 'qwen', 'production')
    assert module.placement([{'name': 'qwen', 'size_vram': 0}], 'qwen', 'production')
    assert not module.placement([{'name': 'qwen'}], 'qwen', 'production')
    assert module.placement([{'name': 'qwen', 'size_vram': 123}], 'qwen', 'development')
