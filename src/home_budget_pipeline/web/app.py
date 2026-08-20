"""Minimal Ledger web/API application served behind oauth2-proxy.

Browser authentication is performed by oauth2-proxy using Microsoft Entra ID.
The proxy forwards authenticated identity headers to this application.  Direct
JWT bearer validation can be added for CLI/API callers without changing the
browser login flow.
"""

from __future__ import annotations

import html
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse

BASE_PATH = os.getenv("LEDGER_BASE_PATH", "/ledger").rstrip("/") or "/ledger"

app = FastAPI(title="BrownRook Ledger", version="0.1.0")


def authenticated_identity(
    x_forwarded_user: Optional[str] = Header(default=None),
    x_forwarded_email: Optional[str] = Header(default=None),
    x_auth_request_user: Optional[str] = Header(default=None),
    x_auth_request_email: Optional[str] = Header(default=None),
) -> dict[str, str]:
    """Return the identity asserted by oauth2-proxy.

    The Ledger service is intentionally ClusterIP-only; public traffic reaches
    it through oauth2-proxy.  Requiring an asserted identity also prevents an
    accidental unauthenticated direct call from being treated as a user session.
    """

    user = x_auth_request_user or x_forwarded_user
    email = x_auth_request_email or x_forwarded_email
    if not user and not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authenticated identity header missing",
        )
    return {
        "user": user or email or "unknown",
        "email": email or "",
    }


@app.get(f"{BASE_PATH}/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ledger"}


@app.get(f"{BASE_PATH}/api/me")
def me(
    x_forwarded_user: Optional[str] = Header(default=None),
    x_forwarded_email: Optional[str] = Header(default=None),
    x_auth_request_user: Optional[str] = Header(default=None),
    x_auth_request_email: Optional[str] = Header(default=None),
) -> dict[str, str]:
    return authenticated_identity(
        x_forwarded_user=x_forwarded_user,
        x_forwarded_email=x_forwarded_email,
        x_auth_request_user=x_auth_request_user,
        x_auth_request_email=x_auth_request_email,
    )


@app.get(BASE_PATH, response_class=HTMLResponse)
@app.get(f"{BASE_PATH}/", response_class=HTMLResponse)
def ledger_home(
    x_forwarded_user: Optional[str] = Header(default=None),
    x_forwarded_email: Optional[str] = Header(default=None),
    x_auth_request_user: Optional[str] = Header(default=None),
    x_auth_request_email: Optional[str] = Header(default=None),
) -> str:
    identity = authenticated_identity(
        x_forwarded_user=x_forwarded_user,
        x_forwarded_email=x_forwarded_email,
        x_auth_request_user=x_auth_request_user,
        x_auth_request_email=x_auth_request_email,
    )
    display = html.escape(identity["email"] or identity["user"])
    return f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>BrownRook Ledger</title></head>
  <body>
    <main>
      <h1>BrownRook Ledger</h1>
      <p>Signed in as {display}</p>
      <p>Ledger web application is running.</p>
    </main>
  </body>
</html>"""


def main() -> None:
    import uvicorn

    uvicorn.run(
        "home_budget_pipeline.web.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
