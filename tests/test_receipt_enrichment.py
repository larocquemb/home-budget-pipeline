import json

import pytest

from home_budget_pipeline import cli, receipt_enrichment
from home_budget_pipeline.receipt_enrichment import receipt_patterns


def test_numeric_receipt_selector_does_not_fall_back_to_filename():
    assert receipt_patterns("1") == ()


def test_receipt_selector_accepts_filename_or_path():
    assert receipt_patterns("2026-08-11/receipt_0023.pdf") == ("%receipt_0023.pdf%",)


def test_receipt_selector_rejects_blank_value():
    with pytest.raises(ValueError, match="receipt selector is required"):
        receipt_patterns("   ")


def test_brownrook_receipt_enrichment_command(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setattr(receipt_enrichment, "resolve_item_ids", lambda dsn, receipt: (11, 12))
    monkeypatch.setattr(
        receipt_enrichment,
        "run",
        lambda **kwargs: {"considered": 2, "accepted": 2},
    )

    assert cli.main(["receipts", "enrich", "--receipt", "1", "--write-db"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "receipt": "1",
        "item_ids": [11, 12],
        "considered": 2,
        "accepted": 2,
    }


def test_receipt_limit_selects_only_first_five_items(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setattr(receipt_enrichment, "resolve_item_ids", lambda dsn, receipt: tuple(range(11, 19)))
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return {"considered": len(kwargs["item_ids"])}
    monkeypatch.setattr(receipt_enrichment, "run", run)
    assert cli.main(["receipts", "enrich", "--receipt", "2026-02-14/20260214_sobeys_363_95.pdf",
                     "--limit", "5", "--write-db"]) == 0
    assert calls[0]["item_ids"] == (11, 12, 13, 14, 15)
    assert calls[0]["limit"] == 5
    assert calls[0]["write_db"] is True
    assert json.loads(capsys.readouterr().out)["item_ids"] == [11, 12, 13, 14, 15]


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_receipt_limit_rejects_nonpositive_values(limit, monkeypatch):
    calls = []
    monkeypatch.setattr(receipt_enrichment, "run", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(SystemExit) as result:
        cli.main(["receipts", "enrich", "--receipt", "1", "--limit", limit])
    assert result.value.code == 2
    assert calls == []
