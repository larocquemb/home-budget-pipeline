import os
from unittest.mock import patch

import pytest

from home_budget_pipeline.config import RuntimePaths


def test_runtime_paths_default_to_container_data_root():
    with patch.dict(os.environ, {}, clear=True):
        paths = RuntimePaths.from_env()
    assert str(paths.data_root) == "/data"
    assert str(paths.receipt_root) == "/data/receipts"
    assert str(paths.raw_receipts) == "/data/receipts/raw"
    assert str(paths.scanned_receipts) == "/data/receipts/raw/scanned"
    assert str(paths.scanned_inbox) == "/data/receipts/raw/scanned/inbox"
    assert str(paths.electronic_receipts) == "/data/receipts/raw/electronic"
    assert str(paths.derived_receipts) == "/data/receipts/derived"
    assert str(paths.ocr_cache) == "/data/cache/ocr"


def test_runtime_paths_can_point_to_pve_smb_mount():
    with patch.dict(
        os.environ,
        {"HOME_BUDGET_DATA_ROOT": "/Volumes/files/brownrook/home-budget"},
        clear=True,
    ):
        paths = RuntimePaths.from_env()
    assert str(paths.scanned_inbox) == "/Volumes/files/brownrook/home-budget/receipts/raw/scanned/inbox"
    assert str(paths.electronic_receipts) == "/Volumes/files/brownrook/home-budget/receipts/raw/electronic"
    assert str(paths.electronic_merchant_dir("Costco")) == "/Volumes/files/brownrook/home-budget/receipts/raw/electronic/Costco"
    assert str(paths.derived_receipts) == "/Volumes/files/brownrook/home-budget/receipts/derived"


def test_merchant_directory_rejects_empty_name():
    with patch.dict(os.environ, {}, clear=True):
        paths = RuntimePaths.from_env()
    with pytest.raises(ValueError):
        paths.electronic_merchant_dir("   ")
