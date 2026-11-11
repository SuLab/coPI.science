"""The sign-in stamp every login writes into the session, and the freshness test that linking a
Microsoft account needs (docs/specs/2026-10-21-microsoft-sign-in-design.md §5.3, §5.4).

Linking needs more than a recent ORCID sign-in: an ordinary one may pass on ORCID's remembered
browser session without a password. Only the /login/start?reauth=1 callback, after its ID token's
`auth_time` passed (src/services/orcid_reauth.py), writes ORCID_REAUTH_AT_KEY. Every sign-in
starts a fresh session, so that key never outlives the sign-in that earned it. A session issued
before these keys existed has none of them and counts as stale.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, MutableMapping
from typing import Any

SIGNED_IN_AT_KEY = "signed_in_at"
SIGNED_IN_VIA_KEY = "signed_in_via"
VIA_ORCID = "orcid"
VIA_MICROSOFT = "microsoft"
ORCID_REAUTH_AT_KEY = "orcid_reauth_at"
#: How recent the proven ORCID re-sign-in must be to link a Microsoft account.
LINK_REAUTH_MAX_AGE_SECONDS = 15 * 60


def record_sign_in(session: MutableMapping[str, Any], via: str, now: int | None = None) -> None:
    session[SIGNED_IN_AT_KEY] = int(time.time()) if now is None else now
    session[SIGNED_IN_VIA_KEY] = via


def record_orcid_reauth(session: MutableMapping[str, Any], now: int | None = None) -> None:
    """Mark this session's ORCID sign-in as a proven re-sign-in. Only the reauth callback,
    after validate_reauth_id_token passed, may call this."""
    session[ORCID_REAUTH_AT_KEY] = int(time.time()) if now is None else now


def fresh_orcid_sign_in(session: Mapping[str, Any], now: float | None = None) -> bool:
    """True when this session began with a proven ORCID re-sign-in at most 15 minutes ago."""
    reauth_at = session.get(ORCID_REAUTH_AT_KEY)
    if (session.get(SIGNED_IN_VIA_KEY) != VIA_ORCID or isinstance(reauth_at, bool)
            or not isinstance(reauth_at, int)):
        return False
    age = (time.time() if now is None else now) - reauth_at
    return 0 <= age <= LINK_REAUTH_MAX_AGE_SECONDS
