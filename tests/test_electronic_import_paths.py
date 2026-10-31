import json
from contextlib import nullcontext

from home_budget_pipeline.receipts import electronic


def test_default_cli_uses_shared_electronic_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('ELECTRONIC_RECEIPT_SOURCE_ROOT', str(tmp_path))
    calls = []
    monkeypatch.setattr(electronic, 'import_receipts',
                        lambda root, **options: calls.append((root, options)) or {'imported': 0})
    electronic.main([])
    assert calls == [(tmp_path, {'require_costco': True})]
    assert json.loads(capsys.readouterr().out) == {'imported': 0}


def test_recursive_import_preserves_paths_and_skips_other_merchants(tmp_path, monkeypatch):
    folder = tmp_path / '2026-10-27'
    folder.mkdir()
    for name in ('costco.PDF', 'other.pdf', '.hidden.pdf'):
        (folder / name).write_text(name)
    (folder / '.hidden').mkdir()
    (folder / '.hidden' / 'receipt.pdf').write_text('hidden')

    class Cursor(list):
        def fetchall(self):
            return self

    class Connection:
        def execute(self, *args):
            return Cursor()

    monkeypatch.setattr(electronic.costco, '_connect_postgres', lambda _: nullcontext(Connection()))
    read = []

    def text(path):
        read.append(path.name)
        return ('COSTCO\n' if path.name == 'costco.PDF' else 'OTHER SHOP\n') + 'SUBTOTAL 5.00\n**** TOTAL 5.00'

    monkeypatch.setattr(electronic.costco, 'pdf_text', text)
    monkeypatch.setattr(electronic.costco, 'parse_receipt_items', lambda *_: [])
    written = []
    monkeypatch.setattr(electronic.costco, 'write_costco_to_postgres', lambda rows, *_: written.extend(rows))
    result = electronic.import_receipts(tmp_path, require_costco=True)
    assert result['imported'] == 1
    assert result['unsupported'] == ['2026-10-27/other.pdf']
    assert written[0]['source_reference'] == 'electronic/2026-10-27/costco.PDF'
    assert sorted(read) == ['costco.PDF', 'other.pdf']
