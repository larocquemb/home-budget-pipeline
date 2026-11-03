"""Inspect or disable the retired development transaction watcher."""
import subprocess
import sys
import receipt_services

receipt_services.NAMES=('transactions',)

if __name__=='__main__':
    if len(sys.argv)<2 or sys.argv[1] not in {'stop','status','logs'}:
        raise SystemExit('Development imports are manual; use the GUI refresh button or ledger transactions scan')
    try:
        raise SystemExit(receipt_services.main())
    except subprocess.CalledProcessError as exc:
        print((exc.stderr or str(exc)).strip())
        raise SystemExit(exc.returncode) from None
