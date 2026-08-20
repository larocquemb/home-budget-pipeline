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

    Receipts are organized by evidence type rather than merchant. Defaults target
    a container/Kubernetes mount at /data. Local development can point the same
    variables at a mounted PVE SMB share without changing application code.
    """

    data_root: Path
    receipt_root: Path
    scanned_inbox: Path
    electronic_inbox: Path
    scanned_archive: Path
    electronic_archive: Path
    output_dir: Path
    ocr_cache: Path

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        data_root = _path_env("HOME_BUDGET_DATA_ROOT", "/data")
        receipt_root = _path_env(
            "HOME_BUDGET_RECEIPT_ROOT",
            str(data_root / "receipts"),
        )
        return cls(
            data_root=data_root,
            receipt_root=receipt_root,
            scanned_inbox=_path_env(
                "HOME_BUDGET_SCANNED_INBOX",
                str(receipt_root / "inbox" / "scanned"),
            ),
            electronic_inbox=_path_env(
                "HOME_BUDGET_ELECTRONIC_INBOX",
                str(receipt_root / "inbox" / "electronic"),
            ),
            scanned_archive=_path_env(
                "HOME_BUDGET_SCANNED_ARCHIVE",
                str(receipt_root / "archive" / "scanned"),
            ),
            electronic_archive=_path_env(
                "HOME_BUDGET_ELECTRONIC_ARCHIVE",
                str(receipt_root / "archive" / "electronic"),
            ),
            output_dir=_path_env("HOME_BUDGET_OUTPUT_DIR", str(data_root / "output")),
            ocr_cache=_path_env("HOME_BUDGET_OCR_CACHE", str(data_root / "cache" / "ocr")),
        )

    def electronic_merchant_archive(self, merchant: str) -> Path:
        """Return a merchant-specific directory beneath the generic electronic archive."""
        name = merchant.strip().replace("/", "_").replace("\\", "_")
        if not name or name in {".", ".."}:
            raise ValueError("merchant must be a non-empty safe directory name")
        return self.electronic_archive / name


def runtime_paths() -> RuntimePaths:
    """Return runtime paths resolved from the current process environment."""
    return RuntimePaths.from_env()
