import os
from unittest.mock import patch

import pytest

from home_budget_pipeline.config import RuntimePaths


def test_runtime_paths_default_to_container_data_root():
    with patch.dict(os.environ, {}, clear=True):
        paths = RuntimePaths.from_env()
    assert str(paths.data_root) == "/data"
    assert str(paths.receipt_root) == "/data/receipts"
    assert str(paths.scanned_inbox) == "/data/receipts/inbox/scanned"
    assert str(paths.electronic_inbox) == "/data/receipts/inbox/electronic"
    assert str(paths.scanned_archive) == "/data/receipts/archive/scanned"
    assert str(paths.electronic_archive) == "/data/receipts/archive/electronic"
    assert str(paths.output_dir) == "/data/output"
    assert str(paths.ocr_cache) == "/data/cache/ocr"


def test_runtime_paths_can_point_to_smb_mount():
    with patch.dict(
        os.environ,
        {"HOME_BUDGET_DATA_ROOT": "/Volumes/home-budget"},
        clear=True,
    ):
        paths = RuntimePaths.from_env()
    assert str(paths.scanned_inbox) == "/Volumes/home-budget/receipts/inbox/scanned"
    assert str(paths.electronic_archive) == "/Volumes/home-budget/receipts/archive/electronic"
    assert str(paths.electronic_merchant_archive("Costco")) == "/Volumes/home-budget/receipts/archive/electronic/Costco"
    assert str(paths.output_dir) == "/Volumes/home-budget/output"


def test_merchant_archive_rejects_empty_name():
    with patch.dict(os.environ, {}, clear=True):
        paths = RuntimePaths.from_env()
    with pytest.raises(ValueError):
        paths.electronic_merchant_archive("   ")
