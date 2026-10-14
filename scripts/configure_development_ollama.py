#!/usr/bin/env python3
"""Update only the local, ignored development Ollama settings."""
import argparse
import os
from pathlib import Path
import re
import tempfile

VALUES = {
    "OLLAMA_HOST": "http://192.168.2.201:11436",
    "OLLAMA_BASE_URL": "http://192.168.2.201:11436",
    "OLLAMA_GPU_METRICS_URL": "http://192.168.2.201:11435/gpu",
}

def update(text):
    for name, value in VALUES.items():
        pattern = rf"(?m)^(?:export[ \t]+)?{name}=.*$"
        if re.search(pattern, text):
            text = re.sub(pattern, f"{name}={value}", text)
        else:
            text = text.rstrip("\n") + f"\n{name}={value}\n"
    return text

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.dev"))
    args = parser.parse_args()
    path = args.env_file
    if path.is_symlink() or path.name != ".env.dev" or not path.is_file():
        parser.error("Use an existing regular .env.dev file; production files are never edited")
    original = path.read_text()
    revised = update(original)
    if revised != original:
        fd, temporary = tempfile.mkstemp(prefix=".env.dev.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                file.write(revised)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print("Development Ollama configured on Arsene port 11436; restart existing worker processes")

if __name__ == "__main__":
    main()
