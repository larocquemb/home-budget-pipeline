from home_budget_pipeline.receipts import backlog_ingest


def test_receipt_root_defaults_from_home_budget_data_root(monkeypatch):
    monkeypatch.delenv("RECEIPT_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("HOME_BUDGET_OCR_CACHE", raising=False)
    monkeypatch.setenv("HOME_BUDGET_DATA_ROOT", "/Volumes/files/brownrook/home-budget")

    args = backlog_ingest.parse_args([])

    assert args.receipt_root == "/Volumes/files/brownrook/home-budget/receipts/raw/scanned/inbox"
    assert args.ocr_cache == "/Volumes/files/brownrook/home-budget/receipts/derived/ocr-cache"


def test_explicit_environment_paths_override_data_root(monkeypatch):
    monkeypatch.setenv("HOME_BUDGET_DATA_ROOT", "/data-root")
    monkeypatch.setenv("RECEIPT_SOURCE_ROOT", "/custom/inbox")
    monkeypatch.setenv("HOME_BUDGET_OCR_CACHE", "/custom/cache")

    args = backlog_ingest.parse_args([])

    assert args.receipt_root == "/custom/inbox"
    assert args.ocr_cache == "/custom/cache"


def test_explicit_receipt_root_argument_overrides_default(monkeypatch):
    monkeypatch.setenv("HOME_BUDGET_DATA_ROOT", "/data-root")

    args = backlog_ingest.parse_args(["/other/inbox", "--workers", "6"])

    assert args.receipt_root == "/other/inbox"
    assert args.workers == 6
