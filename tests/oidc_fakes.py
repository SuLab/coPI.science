"""Test doubles for the OpenID Connect providers (spec
docs/specs/2026-10-21-microsoft-sign-in-design.md §8: no test talks to Microsoft or ORCID): a
signing key, ID tokens signed with it, a self-signed client certificate, and one
httpx.MockTransport answering as Microsoft's token endpoint and per-tenant key sets and as
ORCID's key set. tests/conftest.py registers the `fake_idp` fixture for every test module.
The token endpoint is served through a MockTransport of whichever HTTP library authlib's client
is built on (microsoft_identity.TOKEN_HTTP): authlib 1.8 builds AsyncOAuth2Client on httpx2
when httpx2 is installed, else on httpx (authlib/integrations/httpx_client/_compat.py);
requirements.lock installs no httpx2."""
from __future__ import annotations

import base64
import datetime
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from joserfc import jwt
from joserfc.jwk import RSAKey

TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REDIRECT_URI = "https://copi.example/auth/microsoft/callback"
OID = "0f0f0f0f-1111-2222-3333-444444444444"
KID = "test-key-1"
MICROSOFT_HOST = "login.microsoftonline.com"
ORCID_JWKS_URL = "https://orcid.org/oauth/jwks"
#: Pass as a claim value to `microsoft_claims` to leave that claim out.
DROP = object()


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


SIGNING_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
OTHER_KEY_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def make_certificate(
    *, key_size: int = 2048, days: int = 365, start_days_ago: int = 1
) -> tuple[bytes, bytes]:
    """(certificate PEM, private key PEM) of a fresh self-signed certificate, valid from
    `start_days_ago` days ago until `days` days from now."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "copi-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=start_days_ago))
        .not_valid_after(now + datetime.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), _pem(key)


CERT_PEM, CERT_KEY_PEM = make_certificate()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def public_jwk(kid: str = KID, *, key_pem: bytes = SIGNING_PEM, **extra) -> dict:
    return {**RSAKey.import_key(key_pem).as_dict(private=False), "kid": kid, "use": "sig", **extra}


def sign(claims: dict, *, kid: str | None = KID, alg: str = "RS256",
         key_pem: bytes = SIGNING_PEM) -> str:
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    return jwt.encode(header, claims, RSAKey.import_key(key_pem), algorithms=[alg])


def forged(header: dict, claims: dict) -> str:
    """A compact JWS with any header (e.g. alg=none) and a junk signature."""
    def enc(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
    return f"{enc(header)}.{enc(claims)}.c2lnbmF0dXJl"


def microsoft_claims(*, nonce: str, now: int | None = None, tenant: str = TENANT,
                     **overrides) -> dict:
    now = int(time.time()) if now is None else now
    claims = {
        "iss": f"https://{MICROSOFT_HOST}/{tenant}/v2.0", "aud": CLIENT_ID, "tid": tenant,
        "oid": OID, "sub": "pairwise-subject", "nonce": nonce, "iat": now, "nbf": now,
        "exp": now + 3600, "acct": 0, "preferred_username": "staff@example.edu", "ver": "2.0",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not DROP}


def settings_for(**overrides) -> SimpleNamespace:
    values = dict(
        microsoft_client_id=CLIENT_ID, microsoft_redirect_uri=REDIRECT_URI,
        microsoft_allowed_tenant_ids=TENANT, microsoft_certificate_b64=b64(CERT_PEM),
        microsoft_private_key_b64=b64(CERT_KEY_PEM), allow_http_sessions=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_config(**overrides):
    from src.services.microsoft_identity import load_config

    return load_config(settings_for(**overrides))


class FakeIdP:
    """The handler behind one MockTransport for every provider URL the code calls."""

    def __init__(self) -> None:
        self.token_response: tuple[int, dict] = (500, {"error": "server_error"})
        #: When set, the token endpoint answers (status, raw bytes) as application/json,
        #: whatever token_response says.
        self.token_raw: tuple[int, bytes] | None = None
        self.token_requests: list[httpx.Request] = []
        self.key_fetches: list[str] = []
        self.keys_status = 200
        self.tenant_keys = {
            tid: [public_jwk(issuer=f"https://{MICROSOFT_HOST}/{tid}/v2.0")]
            for tid in (TENANT, OTHER_TENANT)
        }
        self.orcid_keys = [public_jwk()]

    def handler(self, request: httpx.Request, http=httpx) -> httpx.Response:
        """Answer `request`; `http` is the library whose Response the transport expects."""
        url = request.url
        if url.host == MICROSOFT_HOST and url.path == "/organizations/oauth2/v2.0/token":
            self.token_requests.append(request)
            if self.token_raw is not None:
                status, raw = self.token_raw
                return http.Response(status, content=raw,
                                     headers={"content-type": "application/json"})
            status, body = self.token_response
            return http.Response(status, json=body)
        if url.host == MICROSOFT_HOST and url.path.endswith("/discovery/v2.0/keys"):
            self.key_fetches.append(str(url))
            if self.keys_status != 200:
                return httpx.Response(self.keys_status)
            tenant = url.path.split("/")[1]
            return httpx.Response(200, json={"keys": self.tenant_keys.get(tenant, [])})
        if str(url) == ORCID_JWKS_URL:
            self.key_fetches.append(str(url))
            if self.keys_status != 200:
                return httpx.Response(self.keys_status)
            return httpx.Response(200, json={"keys": self.orcid_keys})
        return httpx.Response(404)

    def token_form(self, index: int = -1) -> dict[str, str]:
        body = self.token_requests[index].content.decode()
        return {k: v[0] for k, v in parse_qs(body).items()}


@pytest.fixture
def fake_idp(monkeypatch):
    from src.services import id_tokens, microsoft_identity, orcid_reauth

    fake = FakeIdP()
    monkeypatch.setattr(id_tokens, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    http = microsoft_identity.TOKEN_HTTP
    monkeypatch.setattr(microsoft_identity, "TOKEN_TRANSPORT",
                        http.MockTransport(lambda request: fake.handler(request, http)))
    monkeypatch.setattr(microsoft_identity, "KEYS", id_tokens.KeySetCache())
    monkeypatch.setattr(orcid_reauth, "KEYS", id_tokens.KeySetCache())
    return fake
