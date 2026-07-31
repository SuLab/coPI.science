"""Print a signed ``copi-session`` cookie value for a user id.

Must run where the app's ``SECRET_KEY`` is readable (i.e. inside a container),
because the cookie is only accepted by ``SessionMiddleware`` if it is signed
with that key::

    docker exec -i app-8002 python -m tests.e2e.mint_cookie <user-uuid>

This is the same forgery ``tests/integration/test_cohort_admin.py::_auth`` does,
extracted so the host-side helper can consume it. It is a *test* affordance for
a broken login path, not a production one: it needs the signing key, so it
grants nothing an operator does not already have.
"""

import sys

from tests.e2e.session import forge_session_cookie


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m tests.e2e.mint_cookie <user-uuid>")
    print(forge_session_cookie(sys.argv[1]))


if __name__ == "__main__":
    main()
