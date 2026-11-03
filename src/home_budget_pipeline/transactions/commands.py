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
    imp = sub.add_parser('import-cibc', help='Preview/import a headerless CIBC card or deposit-account CSV (CAD).')
    imp.add_argument('path', type=Path)
    imp.add_argument('--account-reference', required=True, help='Stable account label; reuse it for subsequent downloads.')
    imp.add_argument('--apply', action='store_true')
    imp.add_argument('--account-type',choices=('credit_card','chequing','savings'),default='credit_card')
    imp.add_argument('--account-last4',help='Required for deposit accounts; exactly four digits.')
    imp.add_argument('--currency',choices=('CAD','USD'),default='CAD')
    imp.set_defaults(handler=run)
    tree=sub.add_parser('import-tree',help='Preview/import product/xLAST4/[year/]CSV exports below a CIBC archive root.')
    tree.add_argument('root',type=Path)
    tree.add_argument('--apply',action='store_true')
    tree.add_argument('--account-currency',action='append',default=[],metavar='PRODUCT/xLAST4=USD',
                      help='Explicit currency for an account folder; repeat as needed. Existing accounts retain their registered currency.')
    tree.set_defaults(handler=run)
    matches = sub.add_parser('suggest', help='Read-only receipt candidates; never confirm links or enrich receipts.')
    matches.add_argument('--transaction-id', type=int)
    matches.set_defaults(handler=run)
    split = sub.add_parser('allocate', help='Preview/save a complete balanced allocation with audit history.')
    split.add_argument('transaction_id', type=int)
    split.add_argument('--plan', type=Path, required=True, help='JSON: expected_version, reason, allocations.')
    split.add_argument('--apply', action='store_true')
    split.set_defaults(handler=run)
    migration=sub.add_parser('migrate-receipt-store',help='Preview/copy legacy bank rows into the configured separate accounting database; preserve originals.')
    migration.add_argument('--apply',action='store_true')
    migration.set_defaults(handler=run)
    ownership=sub.add_parser('ownership',help='Record account ownership and linked cardholders using only last-four identifiers.')
    ownership.add_argument('--plan',type=Path,required=True)
    ownership.add_argument('--apply',action='store_true')
    ownership.set_defaults(handler=run)
    for p in (setup, imp, tree, split, migration, ownership):
        p.add_argument('--actor', default=None, help='Required for writes; person responsible for the operation.')


def run(args):
    from ..web.queries import LedgerQueryService
    service = LedgerQueryService()
    from .database import service as accounting_service
    bank=accounting_service()
    action = args.transaction_command
    if action == 'suggest':
        where = ' WHERE id=%s' if args.transaction_id is not None else ''
        rows = bank._fetch('SELECT * FROM accounting.financial_transactions' + where + ' ORDER BY transaction_date DESC,id',
                              (args.transaction_id,) if where else ())
        report = [{'transaction_id': t['id'], 'date': t['transaction_date'], 'amount': t['amount'],
                   'description': t['description'], 'candidates': receipt_candidates(service, t)} for t in rows]
    else:
        if args.apply and not (args.actor or '').strip():
            raise ValueError('--actor is required for writes')
        with bank._connect() as conn:
            if action == 'setup':
                if args.apply:
                    ensure_schema(conn)
                report = {'applied': args.apply, 'operation': 'add transaction import/allocation tables'}
            elif action == 'import-cibc':
                report = import_cibc(conn,args.path,args.account_reference,args.actor or 'preview',apply=args.apply,
                                    account_type=args.account_type,account_last4=args.account_last4,currency=args.currency)
            elif action=='import-tree':
                from .tree_import import import_tree
                currencies={}
                for item in args.account_currency:
                    if '=' not in item:
                        raise ValueError('Currency overrides use product/xLAST4=USD')
                    key,value=item.split('=',1)
                    if key.casefold() in currencies and currencies[key.casefold()]!=value.upper():
                        raise ValueError('Conflicting account currency overrides')
                    currencies[key.casefold()]=value.upper()
                report=import_tree(conn,args.root,args.actor or 'preview',apply=args.apply,currencies=currencies)
            elif action=='migrate-receipt-store':
                from .migrate import migrate
                with service._connect() as source:
                    report=migrate(source,conn,apply=args.apply)
            elif action=='ownership':
                from .accounts import record_ownership
                plan=json.loads(args.plan.read_text())
                if args.apply:
                    report=record_ownership(conn,actor=args.actor,**plan)
                else:
                    report={'applied':False,'operation':'record account/card ownership','account_reference':plan['reference']}
            else:
                plan = json.loads(args.plan.read_text())
                report = save_allocations(conn, args.transaction_id, plan['allocations'],
                                          plan['expected_version'], args.actor or 'preview', plan['reason'], apply=args.apply,
                                          receipt_service=service)
    print(json.dumps(report, default=str, indent=2))
    return 0
