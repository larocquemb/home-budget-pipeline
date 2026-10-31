#!/usr/bin/env python3
"""Control read-only development receipt progress reporting."""
import receipt_services

receipt_services.NAMES = ('progress',)
if __name__ == '__main__':
    raise SystemExit(receipt_services.main())
