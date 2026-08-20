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

    Receipt storage is organized by data lifecycle first (raw vs derived), then
    by evidence type (scanned vs electronic). Defaults target a container or
    Kubernetes mount at /data. Local development can point HOME_BUDGET_DATA_ROOT
    at the same PVE SMB-backed storage without changing application code.
    """

    data_root: Path
    receipt_root: Path
    raw_receipts: Path
    scanned_receipts: Path
    scanned_inbox: Path
    electronic_receipts: Path
    derived_receipts: Path
    ocr_cache: Path

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        data_root = _path_env("HOME_BUDGET_DATA_ROOT", "/data")
        receipt_root = _path_env(
            "HOME_BUDGET_RECEIPT_ROOT",
            str(data_root / "receipts"),
        )
        raw_receipts = _path_env(
            "HOME_BUDGET_RAW_RECEIPTS",
            str(receipt_root / "raw"),
        )
        scanned_receipts = _path_env(
            "HOME_BUDGET_SCANNED_RECEIPTS",
            str(raw_receipts / "scanned"),
        )
        return cls(
            data_root=data_root,
            receipt_root=receipt_root,
            raw_receipts=raw_receipts,
            scanned_receipts=scanned_receipts,
            scanned_inbox=_path_env(
                "HOME_BUDGET_SCANNED_INBOX",
                str(scanned_receipts / "inbox"),
            ),
            electronic_receipts=_path_env(
                "HOME_BUDGET_ELECTRONIC_RECEIPTS",
                str(raw_receipts / "electronic"),
            ),
            derived_receipts=_path_env(
                "HOME_BUDGET_DERIVED_RECEIPTS",
                str(receipt_root / "derived"),
            ),
            ocr_cache=_path_env(
                "HOME_BUDGET_OCR_CACHE",
                str(data_root / "cache" / "ocr"),
            ),
        )

    def electronic_merchant_dir(self, merchant: str) -> Path:
        """Return a merchant-specific directory under generic electronic evidence."""
        name = merchant.strip().replace("/", "_").replace("\\", "_")
        if not name or name in {".", ".."}:
            raise ValueError("merchant must be a non-empty safe directory name")
        return self.electronic_receipts / name


def runtime_paths() -> RuntimePaths:
    """Return runtime paths resolved from the current process environment."""
    return RuntimePaths.from_env()
