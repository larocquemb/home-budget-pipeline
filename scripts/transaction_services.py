"""Control automatic bank-download discovery with the existing native agents."""
import subprocess
import receipt_services

receipt_services.NAMES=('transactions',)

if __name__=='__main__':
    try:
        raise SystemExit(receipt_services.main())
    except subprocess.CalledProcessError as exc:
        print((exc.stderr or str(exc)).strip())
        raise SystemExit(exc.returncode) from None
