"""Optional Ollama request settings; GPU clients retain server defaults."""
import os


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
