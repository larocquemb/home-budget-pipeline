"""Optional Ollama request settings; GPU clients retain server defaults."""
import math
import os


def request_timeout() -> float:
    """Allow CPU Ollama calls more time without extending cloud-provider waits."""
    value = os.getenv("OLLAMA_TIMEOUT_SECONDS", "").strip()
    if not value:
        value = os.getenv("COMPARISON_TIMEOUT_SECONDS", "180")
    try:
        timeout = float(value)
    except ValueError:
        raise ValueError("Ollama timeout must be a positive finite number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Ollama timeout must be a positive finite number")
    return timeout


def thread_options() -> dict:
    value = os.getenv("OLLAMA_NUM_THREADS", "").strip()
    if not value:
        return {}
    try:
        threads = int(value)
    except ValueError:
        raise ValueError("OLLAMA_NUM_THREADS must be a positive integer") from None
    if threads < 1:
        raise ValueError("OLLAMA_NUM_THREADS must be a positive integer")
    return {"num_thread": threads}
