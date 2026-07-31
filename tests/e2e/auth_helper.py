"""One-shot cookie-planting redirector, so a *human's* browser can be logged in.

Why this exists
---------------
Two of the Task 12 flows cannot be driven by the automation browser:

* **Slack OAuth approval.** The Playwright browser has no Slack session, so
  Slack's "Allow" screen cannot be reached, let alone clicked.
* Anything downstream of it, because Slack redirects to
  ``/admin/agents/slack/callback`` which is behind ``get_admin_user``.

So the human drives it in *their* browser, which is signed into Slack. They
still need our admin session cookie, and ORCID login is broken (see
``tests/e2e/README.md``). This server hands it to them: it sets the
pre-signed ``copi-session`` cookie for host ``localhost`` and 302s to the app.

**Cookies ignore port.** A cookie set by ``localhost:8099`` with no ``Domain``
attribute is host-only for ``localhost`` and is therefore sent to
``localhost:8002`` as well. That is the whole trick — this process never needs
to be the app.

Usage (host, stdlib only — no venv needed)::

    # 1. mint the cookie inside a container that has the app's SECRET_KEY
    docker exec -i app-8002 python -m tests.e2e.mint_cookie <user-uuid>

    # 2. serve it
    E2E_SESSION_COOKIE='<value from step 1>' \
    E2E_TARGET_URL='http://localhost:8002/admin/agents' \
    python3 tests/e2e/auth_helper.py

    # 3. give the human http://localhost:8099/

Caveat to tell the human: the cookie is scoped to ``localhost``, so it replaces
any session they had on *any* localhost port, including the 8001 instance.
"""

import http.server
import os
import sys

PORT = int(os.environ.get("E2E_HELPER_PORT", "8099"))
COOKIE_NAME = "copi-session"


class _Handler(http.server.BaseHTTPRequestHandler):
    cookie = ""
    target = ""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") not in ("", "/go"):
            self.send_error(404)
            return
        self.send_response(302)
        # No Domain attribute => host-only cookie for "localhost", which the
        # browser sends to every port on that host.
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={self.cookie}; Path=/; SameSite=Lax; Max-Age=86400",
        )
        self.send_header("Location", self.target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[auth_helper] {fmt % args}\n")


def main() -> None:
    cookie = os.environ.get("E2E_SESSION_COOKIE", "")
    target = os.environ.get("E2E_TARGET_URL", "")
    if not cookie or not target:
        sys.exit("E2E_SESSION_COOKIE and E2E_TARGET_URL are required")
    _Handler.cookie = cookie
    _Handler.target = target
    httpd = http.server.HTTPServer(("127.0.0.1", PORT), _Handler)
    sys.stderr.write(
        f"[auth_helper] http://localhost:{PORT}/ -> sets {COOKIE_NAME} -> {target}\n"
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
