"""Microsoft (Entra ID) sign-in: configuration, the authorization request, the code exchange
with a certificate assertion, ID-token validation, and the account links (spec
docs/specs/2026-10-21-microsoft-sign-in-design.md §5.1-§5.6).

Accounts are found by Microsoft's (tid, oid) pair only, never by email: Microsoft says `email`
is mutable and must not be used for authorization. Only members (`acct` 0) of the tenants in
MICROSOFT_ALLOWED_TENANT_IDS get through, and every account may link one. Nothing here commits.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client, OAuthError
from authlib.oauth2.rfc7523 import PrivateKeyJWT
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User, UserMicrosoftIdentity
from src.services import id_tokens
from src.services.id_tokens import (
    IdTokenError,
    KeySetCache,
    check_audience,
    check_times,
    unverified_parts,
    verify_signature,
)

logger = logging.getLogger(__name__)

# authlib 1.8 builds AsyncOAuth2Client on httpx2 when httpx2 is installed, else on httpx
# (authlib/integrations/httpx_client/_compat.py); requirements.lock installs no httpx2.
# TOKEN_HTTP is whichever this authlib uses: its errors are caught, and a test's
# TOKEN_TRANSPORT must be its MockTransport. Key fetches (id_tokens) always use httpx.
try:
    import httpx2
except ImportError:  # pragma: no cover - depends on the installed packages
    httpx2 = None
TOKEN_HTTP = (httpx2 if httpx2 is not None and issubclass(AsyncOAuth2Client, httpx2.AsyncClient)
              else httpx)
_TOKEN_HTTP_ERRORS = (httpx.HTTPError, TOKEN_HTTP.HTTPError)
#: Tests replace this with a TOKEN_HTTP.MockTransport; None uses the network. Read at call time.
TOKEN_TRANSPORT = None

_LOGIN = "https://login.microsoftonline.com"
AUTHORIZE_URL = f"{_LOGIN}/organizations/oauth2/v2.0/authorize"
TOKEN_URL = f"{_LOGIN}/organizations/oauth2/v2.0/token"
KEYS_URL_TEMPLATE = _LOGIN + "/{tid}/discovery/v2.0/keys"
ISSUER_TEMPLATE = _LOGIN + "/{tid}/v2.0"
SCOPE = "openid profile"
ASSERTION_LIFETIME_SECONDS = 300
CERTIFICATE_WARNING_DAYS = 30
MIN_KEY_BITS = 2048

#: Every user-facing message of the feature, by code: code -> (text, kind), kind one of
#: "error", "success", "info". Spec §5.8 says which page shows each.
MICROSOFT_MESSAGES: dict[str, tuple[str, str]] = {
    "microsoft_cancelled": ("Microsoft sign-in was cancelled or not approved.", "error"),
    "microsoft_error": (
        "Microsoft sign-in failed. If your organisation must approve CoPI first, "
        "ask its IT team.",
        "error",
    ),
    "microsoft_failed": ("Microsoft sign-in failed. Please try again or use ORCID.", "error"),
    "microsoft_interaction_required": (
        "Microsoft needs you to sign in again. Please try again.", "error",
    ),
    "microsoft_not_linked": (
        "No CoPI account is linked to this Microsoft account. Sign in with ORCID, "
        "then link it in Settings.",
        "error",
    ),
    "state_mismatch": ("Security check failed. Please try again.", "error"),
    "microsoft_unlinked": ("Microsoft sign-in removed. Please sign in again.", "info"),
    "linked": (
        "Microsoft sign-in linked. You can now use it on the sign-in page.", "success",
    ),
    "nothing_linked": ("No Microsoft account is linked.", "info"),
    "account_taken": ("That Microsoft account is already linked to a CoPI account.", "error"),
    "user_already_linked": ("Unlink your current Microsoft account first.", "error"),
    "link_refused": ("Sign in with ORCID again, then link your Microsoft account.", "error"),
    "reauth_failed": ("Could not confirm your ORCID sign-in. Please try again.", "error"),
    "impersonating": (
        "Microsoft sign-in changes are disabled while impersonating.", "error",
    ),
}


def message_for(code: str | None) -> tuple[str, str] | None:
    """(text, kind) for a known message code, else None (including for None)."""
    return MICROSOFT_MESSAGES.get(code) if isinstance(code, str) else None


#: Signing keys per allowed tenant (Microsoft: discover and cache keys per tenant).
KEYS = KeySetCache()


class MicrosoftConfigError(ValueError):
    """The MICROSOFT_* settings are incomplete or invalid; the web app refuses to start
    (spec §5.2)."""


class MicrosoftSignInError(Exception):
    """The code exchange failed. The message is for logs only."""


class InteractionRequired(MicrosoftSignInError):
    """The token endpoint answered `interaction_required`: the user must sign in again."""


@dataclass(frozen=True)
class MicrosoftSignInConfig:
    client_id: str
    redirect_uri: str
    allowed_tenant_ids: frozenset[str]
    private_key_pem: bytes = field(repr=False)  # never in a repr, log or traceback local
    x5t_s256: str
    certificate_not_after: datetime


@dataclass(frozen=True)
class MicrosoftIdentity:
    tenant_id: uuid.UUID
    object_id: uuid.UUID
    sign_in_name: str | None


@dataclass(frozen=True)
class RemovedLink:
    tenant_id: str
    sign_in_name: str | None


def _guid(value: object) -> uuid.UUID | None:
    """`value` as a UUID when it is a canonical (hyphenated, any case) GUID string."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value.lower() else None


def _decode_b64(name: str, value: str) -> bytes:
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MicrosoftConfigError(f"{name} is not base64") from exc


def load_config(settings) -> MicrosoftSignInConfig | None:
    """The validated configuration, or None when MICROSOFT_CLIENT_ID is empty (feature off).

    Raises MicrosoftConfigError on anything incomplete or invalid. Called from create_app()
    only, so it stops the web app and never the worker or the agent (spec §5.2).
    """
    client_id = settings.microsoft_client_id.strip()
    if not client_id:
        return None
    others = ("microsoft_redirect_uri", "microsoft_allowed_tenant_ids",
              "microsoft_certificate_b64", "microsoft_private_key_b64")
    missing = [name.upper() for name in others if not getattr(settings, name).strip()]
    if missing:
        raise MicrosoftConfigError(
            "MICROSOFT_CLIENT_ID is set but these are empty: " + ", ".join(missing)
        )

    tenants = set()
    for raw in settings.microsoft_allowed_tenant_ids.split(","):
        if not raw.strip():
            continue
        tenant = _guid(raw.strip())
        if tenant is None:
            raise MicrosoftConfigError(
                f"MICROSOFT_ALLOWED_TENANT_IDS: {raw.strip()[:40]!r} is not a GUID"
            )
        tenants.add(str(tenant))
    if not tenants:
        raise MicrosoftConfigError("MICROSOFT_ALLOWED_TENANT_IDS lists no tenant")

    redirect_uri = settings.microsoft_redirect_uri.strip()
    parts = urlsplit(redirect_uri)
    local_http = (settings.allow_http_sessions and parts.scheme == "http"
                  and parts.hostname == "localhost")
    if not (parts.scheme == "https" or local_http) or not parts.netloc:
        raise MicrosoftConfigError(
            "MICROSOFT_REDIRECT_URI must be https "
            "(http only for localhost with ALLOW_HTTP_SESSIONS=true)"
        )

    cert_pem = _decode_b64("MICROSOFT_CERTIFICATE_B64", settings.microsoft_certificate_b64)
    key_pem = _decode_b64("MICROSOFT_PRIVATE_KEY_B64", settings.microsoft_private_key_b64)
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError as exc:
        raise MicrosoftConfigError("MICROSOFT_CERTIFICATE_B64 is not a PEM certificate") from exc
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise MicrosoftConfigError(
            "MICROSOFT_PRIVATE_KEY_B64 is not an unencrypted PEM private key"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_KEY_BITS:
        raise MicrosoftConfigError(
            f"MICROSOFT_PRIVATE_KEY_B64 must be an RSA key of at least {MIN_KEY_BITS} bits "
            "(Microsoft Entra ID supports only RSA)"
        )
    try:
        cert_public = cert.public_key()
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise MicrosoftConfigError(
            "MICROSOFT_CERTIFICATE_B64 holds a public key of an unsupported type"
        ) from exc
    if (not isinstance(cert_public, rsa.RSAPublicKey)
            or cert_public.public_numbers() != key.public_key().public_numbers()):
        raise MicrosoftConfigError("MICROSOFT_PRIVATE_KEY_B64 does not match the certificate")

    not_after = cert.not_valid_after_utc
    now = datetime.now(UTC)
    if now > not_after or now < cert.not_valid_before_utc:
        # Accepted anyway: every Microsoft sign-in fails until it is replaced, ORCID is unaffected.
        logger.error(
            "The Microsoft sign-in certificate is not valid now (valid %s to %s): Microsoft "
            "sign-ins will fail until a new one is uploaded and .env updated "
            "(runbook: specs/auth-and-user-management.md)",
            cert.not_valid_before_utc.isoformat(), not_after.isoformat(),
        )
    elif not_after - now < timedelta(days=CERTIFICATE_WARNING_DAYS):
        logger.warning(
            "The Microsoft sign-in certificate expires %s: upload a new one and update .env "
            "(runbook: specs/auth-and-user-management.md)", not_after.isoformat(),
        )
    x5t = base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA256())).rstrip(b"=").decode()
    return MicrosoftSignInConfig(
        client_id=client_id, redirect_uri=redirect_uri, allowed_tenant_ids=frozenset(tenants),
        private_key_pem=key_pem, x5t_s256=x5t, certificate_not_after=not_after,
    )


def authorization_url(config: MicrosoftSignInConfig, *, state: str, nonce: str,
                      code_verifier: str, select_account: bool) -> str:
    params = {
        "client_id": config.client_id, "response_type": "code",
        "redirect_uri": config.redirect_uri, "response_mode": "query", "scope": SCOPE,
        "state": state, "nonce": nonce,
        "code_challenge": create_s256_code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if select_account:
        # Linking must never bind whichever account the browser is already signed in to.
        params["prompt"] = "select_account"
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(config: MicrosoftSignInConfig, *, code: str, code_verifier: str) -> dict:
    """The token response for `code`, authenticated with a fresh certificate assertion.

    A new PrivateKeyJWT per call: authlib writes the `jti` into the instance's claims and
    would reuse it, and its defaults (1 h, no `nbf`) are not Microsoft's 5-10 minutes.
    """
    now = int(time.time())
    assertion = PrivateKeyJWT(
        alg="PS256",
        headers={"typ": "JWT", "x5t#S256": config.x5t_s256},
        claims={"jti": uuid.uuid4().hex, "iat": now, "nbf": now,
                "exp": now + ASSERTION_LIFETIME_SECONDS},
    )
    statuses: list[int] = []  # authlib raises only on 5xx; a 4xx HTML page fails in .json()

    async def _record_status(response) -> None:  # a TOKEN_HTTP.Response
        statuses.append(response.status_code)

    try:
        async with AsyncOAuth2Client(
            client_id=config.client_id,
            client_secret=config.private_key_pem,  # PrivateKeyJWT signs with the "secret"
            token_endpoint_auth_method=assertion,
            redirect_uri=config.redirect_uri,
            transport=TOKEN_TRANSPORT,
            timeout=id_tokens.HTTP_TIMEOUT_SECONDS,
            event_hooks={"response": [_record_status]},
        ) as client:
            token = await client.fetch_token(
                TOKEN_URL, code=code, grant_type="authorization_code",
                code_verifier=code_verifier, client_id=config.client_id,
            )
    except OAuthError as exc:
        if exc.error == "interaction_required":
            raise InteractionRequired(exc.error) from exc
        raise MicrosoftSignInError(f"token endpoint error={exc.error}") from exc
    except (*_TOKEN_HTTP_ERRORS, ValueError, TypeError, AttributeError, OverflowError) as exc:
        status = statuses[-1] if statuses else None
        raise MicrosoftSignInError(
            f"token request failed: {type(exc).__name__} status={status}") from exc
    if not isinstance(token, dict):
        raise MicrosoftSignInError("token response is not an object")
    return dict(token)


async def validate_id_token(config: MicrosoftSignInConfig, id_token: object, *, nonce: str,
                            now: int) -> MicrosoftIdentity:
    """The member identity in `id_token`, or IdTokenError (spec §5.6, steps in order)."""
    if not isinstance(id_token, str) or not id_token:
        raise IdTokenError("missing", "no id_token in the token response")
    header, unverified = unverified_parts(id_token)
    raw_tid = unverified.get("tid")
    tenant = _guid(raw_tid)
    if tenant is None or not isinstance(raw_tid, str):  # _guid accepts only a str
        raise IdTokenError("tid", "not a GUID")
    if str(tenant) not in config.allowed_tenant_ids:
        raise IdTokenError("tenant", f"{tenant} is not allowed")
    try:
        return await _validate_allowed(config, id_token, header, raw_tid, tenant,
                                       nonce=nonce, now=now)
    except IdTokenError as exc:
        exc.tid = str(tenant)  # §5.8: a failed check is logged with its tenant
        raise


async def _validate_allowed(config: MicrosoftSignInConfig, id_token: str, header: dict,
                            raw_tid: str, tenant: uuid.UUID, *, nonce: str,
                            now: int) -> MicrosoftIdentity:
    """validate_id_token's checks from the key fetch on, for a tenant already on the list."""
    jwk = await KEYS.key_for(KEYS_URL_TEMPLATE.format(tid=tenant), header.get("kid"), now=now)
    claims = verify_signature(id_token, jwk)
    expected_issuer = ISSUER_TEMPLATE.format(tid=raw_tid)
    if claims.get("iss") != expected_issuer:
        raise IdTokenError("iss", repr(claims.get("iss"))[:120])
    key_issuer = jwk.get("issuer")
    if (not isinstance(key_issuer, str)
            or re.sub(r"\{tenantid\}", raw_tid, key_issuer, flags=re.IGNORECASE)
            != expected_issuer):
        raise IdTokenError("key_issuer", repr(key_issuer)[:120])
    check_audience(claims, config.client_id)
    check_times(claims, now)
    if not nonce or claims.get("nonce") != nonce:
        raise IdTokenError("nonce", "mismatch")
    object_id = _guid(claims.get("oid"))
    if object_id is None:
        raise IdTokenError("oid", "missing or not a GUID")
    acct = claims.get("acct")
    if isinstance(acct, bool) or acct not in (0, "0"):
        raise IdTokenError("acct", f"not a member: {acct!r}"[:60])
    name = claims.get("preferred_username")
    return MicrosoftIdentity(
        tenant_id=tenant, object_id=object_id,
        sign_in_name=name[:255] if isinstance(name, str) and name else None,
    )


async def find_linked_user(db: AsyncSession, identity: MicrosoftIdentity) -> User | None:
    return await db.scalar(
        select(User)
        .join(UserMicrosoftIdentity, UserMicrosoftIdentity.user_id == User.id)
        .where(UserMicrosoftIdentity.tenant_id == identity.tenant_id,
               UserMicrosoftIdentity.object_id == identity.object_id)
    )


async def record_sign_in(db: AsyncSession, user_id: uuid.UUID,
                         identity: MicrosoftIdentity) -> bool:
    """Stamp the link's last use. False when that link is gone: it was removed (and perhaps
    replaced by another) after find_linked_user read it, and the sign-in must not go ahead
    through a revoked link."""
    result = await db.execute(
        update(UserMicrosoftIdentity)
        .where(UserMicrosoftIdentity.user_id == user_id,
               UserMicrosoftIdentity.tenant_id == identity.tenant_id,
               UserMicrosoftIdentity.object_id == identity.object_id)
        .values(last_sign_in_at=func.now())
        .returning(UserMicrosoftIdentity.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


async def get_link(db: AsyncSession, user_id: uuid.UUID) -> UserMicrosoftIdentity | None:
    return await db.scalar(
        select(UserMicrosoftIdentity).where(UserMicrosoftIdentity.user_id == user_id)
    )


async def link_account(db: AsyncSession, user_id: uuid.UUID,
                       identity: MicrosoftIdentity) -> str | None:
    """Insert the link. On refusal, the MICROSOFT_MESSAGES code: "user_already_linked",
    "account_taken", or "link_refused" when neither holds (the user was deleted meanwhile, a
    foreign-key violation). A concurrent link of the same account or user meets the unique
    constraints inside a savepoint."""
    try:
        async with db.begin_nested():
            db.add(UserMicrosoftIdentity(
                user_id=user_id, tenant_id=identity.tenant_id,
                object_id=identity.object_id, sign_in_name=identity.sign_in_name,
            ))
            await db.flush()
    except IntegrityError:
        if await get_link(db, user_id) is not None:
            return "user_already_linked"
        taken = await db.scalar(
            select(UserMicrosoftIdentity.id).where(
                UserMicrosoftIdentity.tenant_id == identity.tenant_id,
                UserMicrosoftIdentity.object_id == identity.object_id,
            )
        )
        return "account_taken" if taken is not None else "link_refused"
    return None


async def link_view(db: AsyncSession, user_id: uuid.UUID) -> dict | None:
    """The link as plain values for templates, or None."""
    row = await get_link(db, user_id)
    if row is None:
        return None
    return {
        "sign_in_name": row.sign_in_name, "tenant_id": str(row.tenant_id),
        "linked_at": row.linked_at, "last_sign_in_at": row.last_sign_in_at,
    }


async def unlink(db: AsyncSession, user_id: uuid.UUID) -> RemovedLink | None:
    row = await get_link(db, user_id)
    if row is None:
        return None
    removed = RemovedLink(tenant_id=str(row.tenant_id), sign_in_name=row.sign_in_name)
    await db.execute(sa_delete(UserMicrosoftIdentity).where(UserMicrosoftIdentity.id == row.id))
    return removed
