#!/usr/bin/env python3
"""Manage the local Ledger web server as a per-user macOS LaunchAgent."""
import subprocess

import receipt_services

receipt_services.NAMES = ('web',)

if __name__ == '__main__':
    try:
        raise SystemExit(receipt_services.main())
    except subprocess.CalledProcessError as exc:
        print((exc.stderr or str(exc)).strip())
        raise SystemExit(exc.returncode) from None
