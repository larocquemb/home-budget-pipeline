"""Local endpoint changes preserve credentials and unrelated developer settings."""
import importlib.util
from pathlib import Path


def test_local_environment_update_is_idempotent_and_preserves_other_settings():
    path = Path(__file__).parents[1] / "scripts/configure_development_ollama.py"
    spec = importlib.util.spec_from_file_location("configure_development_ollama", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = "# private settings\nDATABASE_URL=postgresql://unchanged\nexport OLLAMA_HOST=http://old:11434\nBRAVE_SEARCH_API_KEY=unchanged\n"
    result = module.update(original)
    assert "DATABASE_URL=postgresql://unchanged\n" in result
    assert "BRAVE_SEARCH_API_KEY=unchanged\n" in result
    assert "OLLAMA_HOST=http://192.168.2.201:11434\n" in result
    assert "http://old" not in result
    assert module.update(result) == result
