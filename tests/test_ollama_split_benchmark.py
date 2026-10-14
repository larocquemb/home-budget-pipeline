"""A benchmark must not report a missing or GPU-backed production model as CPU-only."""
import importlib.util
from pathlib import Path


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
