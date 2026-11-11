"""Signature, audience and time checks for OpenID Connect ID tokens, and a cache of the signing
keys they are checked against (spec docs/specs/2026-10-21-microsoft-sign-in-design.md
§5.6, §5.7).

Shared by Microsoft sign-in (src/services/microsoft_identity.py) and the ORCID re-sign-in
(src/services/orcid_reauth.py); each checks its own issuer, nonce and extra claims. RS256 is
pinned here, never read from the token: neither provider's keys carry an `alg`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
LEEWAY_SECONDS = 300
#: Microsoft's key-rollover guidance: refresh hourly, keep keys for at most 24 hours.
KEY_REFRESH_SECONDS = 3600
KEY_TTL_SECONDS = 24 * 3600
UNKNOWN_KID_REFETCH_SECONDS = 300
#: After a failed refresh served the cached set, how long before the next attempt: an outage
#: must not make every sign-in wait out HTTP_TIMEOUT_SECONDS again.
FAILED_REFRESH_RETRY_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10.0

#: Tests replace this with an httpx.MockTransport; None uses the network. Read at call time.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


class IdTokenError(Exception):
    """A token or key check failed. `check` names it; the message is for logs, never users.
    `tid` is the token's tenant when a caller knows it (Microsoft sign-in logs it)."""

    tid: str | None = None

    def __init__(self, check: str, detail: str = "") -> None:
        super().__init__(f"{check}: {detail}" if detail else check)
        self.check = check


def _segment(part: str) -> dict:
    try:
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise IdTokenError("malformed", type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise IdTokenError("malformed", "segment is not a JSON object")
    return value


def unverified_parts(token: object) -> tuple[dict, dict]:
    """(header, claims), checking nothing but the shape and `alg`. Only for choosing keys."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise IdTokenError("malformed", "not a compact JWS")
    header, claims = _segment(parts[0]), _segment(parts[1])
    if header.get("alg") != ALGORITHM:
        raise IdTokenError("alg", repr(header.get("alg"))[:40])
    return header, claims


def verify_signature(token: str, jwk: dict) -> dict:
    """The token's claims after an RS256 signature check against `jwk`."""
    try:
        return dict(jwt.decode(token, RSAKey.import_key(jwk), algorithms=[ALGORITHM]).claims)
    except (JoseError, ValueError, TypeError) as exc:
        raise IdTokenError("signature", type(exc).__name__) from exc


def check_audience(claims: dict, client_id: str) -> None:
    """`aud` is our client id (or a one-element list holding it). Fails closed on an empty id."""
    aud = claims.get("aud")
    if client_id and (aud == client_id or aud == [client_id]):
        return
    raise IdTokenError("aud", repr(aud)[:80])


def _int_claim(claims: dict, name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdTokenError(name, f"not an integer: {value!r}"[:80])
    return value


def check_times(claims: dict, now: int) -> None:
    """`exp` and `iat` required, `nbf` optional, each with LEEWAY_SECONDS of clock skew."""
    exp = _int_claim(claims, "exp")
    iat = _int_claim(claims, "iat")
    nbf = _int_claim(claims, "nbf") if claims.get("nbf") is not None else None
    if exp < now - LEEWAY_SECONDS:
        raise IdTokenError("exp", "expired")
    if iat > now + LEEWAY_SECONDS:
        raise IdTokenError("iat", "issued in the future")
    if nbf is not None and nbf > now + LEEWAY_SECONDS:
        raise IdTokenError("nbf", "not yet valid")


async def _fetch_keys(url: str) -> dict[str, dict]:
    try:
        async with httpx.AsyncClient(
            transport=HTTP_TRANSPORT, timeout=HTTP_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        raise IdTokenError("keys", f"{type(exc).__name__} status={status} fetching {url}") from exc
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list):
        raise IdTokenError("keys", f"no key list at {url}")
    return {
        k["kid"]: k for k in keys
        if isinstance(k, dict) and isinstance(k.get("kid"), str) and k.get("kty") == "RSA"
    }


@dataclass
class _Entry:
    keys: dict[str, dict]
    fetched_at: float
    last_forced: float
    retry_at: float = float("-inf")


class KeySetCache:
    """Signing keys per key-set URL: refreshed every KEY_REFRESH_SECONDS, served from cache for
    up to KEY_TTL_SECONDS when a refresh fails, refetched on an unknown `kid` at most once per
    UNKNOWN_KID_REFETCH_SECONDS, and not retried for FAILED_REFRESH_RETRY_SECONDS after a failed
    fetch, with or without a cached set to fall back on. In-process and best-effort: concurrent
    misses may both fetch."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        #: When a fetch last failed with no usable cached set, per URL.
        self._failed_at: dict[str, float] = {}

    async def key_for(self, url: str, kid: str | None, *, now: float) -> dict:
        entry = self._entries.get(url)
        usable = entry is not None and now - entry.fetched_at < KEY_TTL_SECONDS
        failed_at = self._failed_at.get(url, float("-inf"))
        if not usable and now - failed_at < FAILED_REFRESH_RETRY_SECONDS:
            raise IdTokenError("keys", f"{url} failed under {FAILED_REFRESH_RETRY_SECONDS} s ago")
        if (entry is None or now - entry.fetched_at >= KEY_TTL_SECONDS
                or (now - entry.fetched_at >= KEY_REFRESH_SECONDS and now >= entry.retry_at)):
            entry = await self._refresh(url, now, entry, forced=False)
        jwk = self._pick(entry, kid)
        if (jwk is None and entry.fetched_at < now and now >= entry.retry_at
                and now - entry.last_forced >= UNKNOWN_KID_REFETCH_SECONDS):
            entry = await self._refresh(url, now, entry, forced=True)
            jwk = self._pick(entry, kid)
        if jwk is None:
            raise IdTokenError("kid", f"no key {kid!r} at {url}")
        return jwk

    @staticmethod
    def _pick(entry: _Entry, kid: str | None) -> dict | None:
        if isinstance(kid, str) and kid:
            return entry.keys.get(kid)
        # A token without `kid` is accepted only against a one-key set (ORCID publishes one).
        return next(iter(entry.keys.values())) if len(entry.keys) == 1 else None

    async def _refresh(
        self, url: str, now: float, previous: _Entry | None, *, forced: bool
    ) -> _Entry:
        try:
            keys = await _fetch_keys(url)
        except IdTokenError as exc:
            if previous is not None and now - previous.fetched_at < KEY_TTL_SECONDS:
                logger.warning(
                    "Signing keys at %s could not be refreshed (%s); using the cached set", url, exc
                )
                if forced:
                    previous.last_forced = now
                previous.retry_at = now + FAILED_REFRESH_RETRY_SECONDS
                return previous
            self._failed_at[url] = now
            raise
        last_forced = now if forced else (previous.last_forced if previous else float("-inf"))
        entry = _Entry(keys=keys, fetched_at=now, last_forced=last_forced)
        self._entries[url] = entry
        return entry
