"""Ledger transaction operations; previews are the default."""
import json
from pathlib import Path

from .workflows import ensure_schema, import_cibc, save_allocations, receipt_candidates


def add_commands(commands):
    parser = commands.add_parser('transactions', help='Import bank records, suggest receipt links and record balanced splits.')
    sub = parser.add_subparsers(dest='transaction_command', required=True)
    setup = sub.add_parser('setup', help='Add bank provenance and allocation tables without rebuilding data.')
    setup.add_argument('--apply', action='store_true')
    setup.set_defaults(handler=run)
    imp = sub.add_parser('import-cibc', help='Preview/import a headerless five-column CIBC card CSV (CAD).')
    imp.add_argument('path', type=Path)
    imp.add_argument('--account-reference', required=True, help='Stable account label; reuse it for subsequent downloads.')
    imp.add_argument('--apply', action='store_true')
    imp.set_defaults(handler=run)
    matches = sub.add_parser('suggest', help='Read-only receipt candidates; never confirm links or enrich receipts.')
    matches.add_argument('--transaction-id', type=int)
    matches.set_defaults(handler=run)
    split = sub.add_parser('allocate', help='Preview/save a complete balanced allocation with audit history.')
    split.add_argument('transaction_id', type=int)
    split.add_argument('--plan', type=Path, required=True, help='JSON: expected_version, reason, allocations.')
    split.add_argument('--apply', action='store_true')
    split.set_defaults(handler=run)
    for p in (setup, imp, split):
        p.add_argument('--actor', default=None, help='Required for writes; person responsible for the operation.')


def run(args):
    from ..web.queries import LedgerQueryService
    service = LedgerQueryService()
    action = args.transaction_command
    if action == 'suggest':
        where = ' WHERE id=%s' if args.transaction_id is not None else ''
        rows = service._fetch('SELECT * FROM budget.financial_transactions' + where + ' ORDER BY transaction_date DESC,id',
                              (args.transaction_id,) if where else ())
        report = [{'transaction_id': t['id'], 'date': t['transaction_date'], 'amount': t['amount'],
                   'description': t['description'], 'candidates': receipt_candidates(service, t)} for t in rows]
    else:
        if args.apply and not (args.actor or '').strip():
            raise ValueError('--actor is required for writes')
        with service._connect() as conn:
            if action == 'setup':
                if args.apply:
                    ensure_schema(conn)
                report = {'applied': args.apply, 'operation': 'add transaction import/allocation tables'}
            elif action == 'import-cibc':
                report = import_cibc(conn, args.path, args.account_reference, args.actor or 'preview', apply=args.apply)
            else:
                plan = json.loads(args.plan.read_text())
                report = save_allocations(conn, args.transaction_id, plan['allocations'],
                                          plan['expected_version'], args.actor or 'preview', plan['reason'], apply=args.apply)
    print(json.dumps(report, default=str, indent=2))
    return 0
