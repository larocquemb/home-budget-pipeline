"""One bounded automatic scan; launchd invokes this without overlapping jobs."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main():
    from home_budget_pipeline.cli_environment import read_environment
    from home_budget_pipeline.transactions.database import connect
    from home_budget_pipeline.transactions.discovery import scan,source_root
    os.environ.update(read_environment(ROOT/'.env.dev'))
    with connect() as conn:
        result=scan(conn,source_root(),'service:transaction-discovery',
                    settle_seconds=float(os.getenv('TRANSACTION_DISCOVERY_SETTLE_SECONDS','10')))
    summary={key:value for key,value in result.items() if key!='files'}
    summary['events']=[row for row in result.get('files',[]) if row['status'] not in {'unchanged','retry_wait'}]
    print(json.dumps(summary,default=str),flush=True)
    return 1 if result.get('failed') else 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__}),flush=True)
        raise SystemExit(1) from None
