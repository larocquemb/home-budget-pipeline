import os
from unittest.mock import patch

from home_budget_pipeline.config import RuntimePaths


def test_runtime_paths_default_to_container_data_root():
    with patch.dict(os.environ, {}, clear=True):
        paths = RuntimePaths.from_env()
    assert str(paths.data_root) == "/data"
    assert str(paths.receipt_inbox) == "/data/receipts/inbox"
    assert str(paths.receipt_archive) == "/data/receipts/archive"
    assert str(paths.output_dir) == "/data/output"
    assert str(paths.ocr_cache) == "/data/cache/ocr"


def test_runtime_paths_can_point_to_smb_mount():
    with patch.dict(
        os.environ,
        {"HOME_BUDGET_DATA_ROOT": "/Volumes/home-budget"},
        clear=True,
    ):
        paths = RuntimePaths.from_env()
    assert str(paths.receipt_inbox) == "/Volumes/home-budget/receipts/inbox"
    assert str(paths.output_dir) == "/Volumes/home-budget/output"
