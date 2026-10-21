"""The ORCID re-sign-in that linking a Microsoft account requires (design decision B4; spec
docs/specs/2026-10-21-microsoft-sign-in-design.md §5.7).

The reauth asks ORCID for `scope=openid` with `prompt=login`, which ORCID documents as forcing
an already signed-in user to reauthenticate. Its ID token proves it: signed by ORCID, for our
client, carrying our nonce, for the signed-in user's ORCID iD, with `auth_time` at most
REAUTH_MAX_AGE_SECONDS old. Ordinary ORCID sign-in (scope /authenticate) never comes here.
"""

from __future__ import annotations

from src.services.id_tokens import (
    LEEWAY_SECONDS,
    IdTokenError,
    KeySetCache,
    check_audience,
    check_times,
    unverified_parts,
    verify_signature,
)

#: From ORCID's live discovery document (https://orcid.org/.well-known/openid-configuration).
ORCID_ISSUER = "https://orcid.org"
ORCID_KEYS_URL = "https://orcid.org/oauth/jwks"
REAUTH_MAX_AGE_SECONDS = 300

KEYS = KeySetCache()


async def validate_reauth_id_token(id_token: object, *, client_id: str, nonce: str | None,
                                   orcid: str, now: int) -> None:
    """Return when the token proves `orcid` just re-entered their ORCID credentials."""
    if not isinstance(id_token, str) or not id_token:
        raise IdTokenError("missing", "no id_token in ORCID's token response")
    header, _ = unverified_parts(id_token)
    jwk = await KEYS.key_for(ORCID_KEYS_URL, header.get("kid"), now=now)
    claims = verify_signature(id_token, jwk)
    if claims.get("iss") != ORCID_ISSUER:
        raise IdTokenError("iss", repr(claims.get("iss"))[:80])
    check_audience(claims, client_id)
    check_times(claims, now)
    if not nonce or claims.get("nonce") != nonce:
        raise IdTokenError("nonce", "mismatch")
    if claims.get("sub") != orcid:
        raise IdTokenError("sub", "not the signed-in user's ORCID iD")
    auth_time = claims.get("auth_time")
    if (isinstance(auth_time, bool) or not isinstance(auth_time, int)
            or not now - REAUTH_MAX_AGE_SECONDS <= auth_time <= now + LEEWAY_SECONDS):
        raise IdTokenError("auth_time", f"{auth_time!r}"[:40])
