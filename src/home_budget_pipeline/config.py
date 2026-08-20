"""Runtime configuration for local, container, and Kubernetes execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser()


@dataclass(frozen=True)
class RuntimePaths:
    """Filesystem paths used by pipeline workloads.

    Defaults target a container/Kubernetes mount at /data. Local development can
    point the same variables at a mounted PVE SMB share without changing code.
    """

    data_root: Path
    receipt_inbox: Path
    receipt_archive: Path
    output_dir: Path
    ocr_cache: Path

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        data_root = _path_env("HOME_BUDGET_DATA_ROOT", "/data")
        return cls(
            data_root=data_root,
            receipt_inbox=_path_env("HOME_BUDGET_RECEIPT_INBOX", str(data_root / "receipts" / "inbox")),
            receipt_archive=_path_env("HOME_BUDGET_RECEIPT_ARCHIVE", str(data_root / "receipts" / "archive")),
            output_dir=_path_env("HOME_BUDGET_OUTPUT_DIR", str(data_root / "output")),
            ocr_cache=_path_env("HOME_BUDGET_OCR_CACHE", str(data_root / "cache" / "ocr")),
        )


def runtime_paths() -> RuntimePaths:
    """Return runtime paths resolved from the current process environment."""
    return RuntimePaths.from_env()
