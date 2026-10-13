"""src/services/microsoft_identity.py (spec 2026-10-13 §5.2, §5.4, §5.5)."""
import base64
import hashlib
import json
import logging
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from joserfc import jwt
from joserfc.jwk import RSAKey

from src.services import microsoft_identity as mi
from src.services.id_tokens import IdTokenError
from tests.oidc_fakes import (
    CERT_KEY_PEM,
    CERT_PEM,
    CLIENT_ID,
    DROP,
    OID,
    OTHER_TENANT,
    REDIRECT_URI,
    TENANT,
    b64,
    forged,
    make_certificate,
    make_config,
    microsoft_claims,
    public_jwk,
    settings_for,
    sign,
)

NOW = int(time.time())


# --- configuration ---------------------------------------------------------------

def test_an_empty_client_id_means_off():
    assert mi.load_config(settings_for(microsoft_client_id="")) is None
    assert mi.load_config(settings_for(microsoft_client_id="  ")) is None


@pytest.mark.parametrize("field", [
    "microsoft_redirect_uri", "microsoft_allowed_tenant_ids",
    "microsoft_certificate_b64", "microsoft_private_key_b64",
])
def test_a_partial_configuration_refuses_to_start(field):
    with pytest.raises(mi.MicrosoftConfigError, match=field.upper()):
        mi.load_config(settings_for(**{field: ""}))


@pytest.mark.parametrize("tenants", ["not-a-guid", f"{TENANT},nope", " , "])
def test_bad_tenant_lists_refuse_to_start(tenants):
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_allowed_tenant_ids=tenants))


def test_tenant_ids_are_normalised():
    config = mi.load_config(settings_for(microsoft_allowed_tenant_ids=f" {TENANT.upper()} , {OTHER_TENANT}"))
    assert config.allowed_tenant_ids == frozenset({TENANT, OTHER_TENANT})


def test_the_redirect_uri_must_be_https_except_local_development():
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_redirect_uri="http://blackbird.example/cb"))
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_redirect_uri="http://localhost:8000/cb"))
    assert mi.load_config(settings_for(microsoft_redirect_uri="http://localhost:8000/cb",
                                       allow_http_sessions=True)) is not None


def test_the_key_must_match_the_certificate_and_be_rsa_2048_or_more():
    _, other_key = make_certificate()
    with pytest.raises(mi.MicrosoftConfigError, match="does not match"):
        mi.load_config(settings_for(microsoft_private_key_b64=b64(other_key)))
    small_cert, small_key = make_certificate(key_size=1024)
    with pytest.raises(mi.MicrosoftConfigError, match="2048"):
        mi.load_config(settings_for(microsoft_certificate_b64=b64(small_cert),
                                    microsoft_private_key_b64=b64(small_key)))
    with pytest.raises(mi.MicrosoftConfigError):
        mi.load_config(settings_for(microsoft_certificate_b64="not base64!"))


def test_the_thumbprint_is_the_base64url_sha256_of_the_certificate_der():
    der = x509.load_pem_x509_certificate(CERT_PEM).public_bytes(serialization.Encoding.DER)
    expected = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    assert make_config().x5t_s256 == expected


def test_a_certificate_near_expiry_warns(caplog):
    cert, key = make_certificate(days=10)
    with caplog.at_level(logging.WARNING, logger=mi.__name__):
        mi.load_config(settings_for(microsoft_certificate_b64=b64(cert), microsoft_private_key_b64=b64(key)))
    assert "expires" in caplog.text


def test_the_private_key_never_appears_in_a_repr():
    config = make_config()
    assert b"PRIVATE KEY" in config.private_key_pem
    assert "PRIVATE KEY" not in repr(config)


def test_only_admins_managers_and_reviewers_may_use_it():
    from types import SimpleNamespace
    assert [r for r in ("pi", "manager", "admin", "reviewer")
            if mi.may_use_microsoft_sign_in(SimpleNamespace(user_role=r))] == ["manager", "admin", "reviewer"]


# --- the authorization request ---------------------------------------------------

def test_the_authorization_url():
    url = mi.authorization_url(make_config(), state="st", nonce="nn", code_verifier="v" * 64,
                               select_account=False)
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == mi.AUTHORIZE_URL
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(("v" * 64).encode()).digest()).rstrip(b"=").decode()
    assert q == {
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "response_mode": "query", "scope": "openid profile", "state": "st", "nonce": "nn",
        "code_challenge": expected_challenge, "code_challenge_method": "S256",
    }
    linked = mi.authorization_url(make_config(), state="st", nonce="nn", code_verifier="v" * 64,
                                  select_account=True)
    assert parse_qs(urlsplit(linked).query)["prompt"] == ["select_account"]


# --- the code exchange -----------------------------------------------------------

async def test_the_code_exchange_sends_a_certificate_assertion(fake_idp):
    fake_idp.token_response = (200, {"token_type": "Bearer", "access_token": "opaque", "id_token": "x"})
    config = make_config()
    tokens = await mi.exchange_code(config, code="the-code", code_verifier="v" * 64)
    await mi.exchange_code(config, code="the-code", code_verifier="v" * 64)
    assert tokens["id_token"] == "x"
    form = fake_idp.token_form(0)
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["code_verifier"] == "v" * 64 and form["client_id"] == CLIENT_ID
    assert form["redirect_uri"] == REDIRECT_URI
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    assert "client_secret" not in form
    assertion = form["client_assertion"]
    header = json.loads(base64.urlsafe_b64decode(assertion.split(".")[0] + "=="))
    assert header == {"alg": "PS256", "typ": "JWT", "x5t#S256": config.x5t_s256}
    claims = jwt.decode(assertion, RSAKey.import_key(CERT_KEY_PEM), algorithms=["PS256"]).claims
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["aud"] == mi.TOKEN_URL
    assert claims["exp"] - claims["nbf"] == 300 and claims["iat"] == claims["nbf"]
    second = fake_idp.token_form(1)["client_assertion"]
    second_jti = jwt.decode(second, RSAKey.import_key(CERT_KEY_PEM), algorithms=["PS256"]).claims["jti"]
    assert second_jti != claims["jti"]


async def test_a_non_json_4xx_from_the_token_endpoint_logs_its_status(fake_idp, monkeypatch):
    def html_403(request):
        return mi.TOKEN_HTTP.Response(403, text="<html>blocked</html>")

    monkeypatch.setattr(mi, "TOKEN_TRANSPORT", mi.TOKEN_HTTP.MockTransport(html_403))
    with pytest.raises(mi.MicrosoftSignInError) as e:
        await mi.exchange_code(make_config(), code="c", code_verifier="v" * 64)
    assert "status=403" in str(e.value)


async def test_interaction_required_is_its_own_error(fake_idp):
    fake_idp.token_response = (400, {"error": "interaction_required", "error_description": "AADSTS50076"})
    with pytest.raises(mi.InteractionRequired):
        await mi.exchange_code(make_config(), code="c", code_verifier="v" * 64)


@pytest.mark.parametrize("response", [(400, {"error": "invalid_grant"}), (503, {}), (200, ["x"])])
async def test_other_token_failures_are_sign_in_errors(fake_idp, response):
    fake_idp.token_response = response
    with pytest.raises(mi.MicrosoftSignInError):
        await mi.exchange_code(make_config(), code="c", code_verifier="v" * 64)


# --- the ID token ----------------------------------------------------------------

async def test_a_valid_token(fake_idp):
    identity = await mi.validate_id_token(
        make_config(), sign(microsoft_claims(nonce="nn", now=NOW)), nonce="nn", now=NOW)
    assert identity == mi.MicrosoftIdentity(
        tenant_id=uuid.UUID(TENANT), object_id=uuid.UUID(OID), sign_in_name="staff@example.edu")
    assert fake_idp.key_fetches == [mi.KEYS_URL_TEMPLATE.format(tid=TENANT)]


@pytest.mark.parametrize("overrides, check", [
    ({"tid": "not-a-guid"}, "tid"),
    ({"tid": OTHER_TENANT, "iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"}, "tenant"),
    ({"iss": "https://login.microsoftonline.com/common/v2.0"}, "iss"),
    ({"iss": f"https://sts.windows.net/{TENANT}/"}, "iss"),
    ({"aud": "someone-else"}, "aud"),
    ({"exp": NOW - 301}, "exp"),
    ({"nbf": NOW + 301}, "nbf"),
    ({"nonce": "other"}, "nonce"),
    ({"nonce": DROP}, "nonce"),
    ({"oid": DROP}, "oid"),
    ({"oid": "nope"}, "oid"),
    ({"acct": 1}, "acct"),
    ({"acct": "1"}, "acct"),
    ({"acct": DROP}, "acct"),
    ({"acct": False}, "acct"),
])
async def test_each_failed_check_refuses(fake_idp, overrides, check):
    token = sign(microsoft_claims(now=NOW, **{"nonce": "nn", **overrides}))
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    assert e.value.check == check
    assert e.value.tid == (None if check in ("tid", "tenant") else TENANT)  # logged (§5.6)


async def test_a_tenant_off_the_list_causes_no_key_fetch(fake_idp):
    token = sign(microsoft_claims(nonce="nn", now=NOW, tenant=OTHER_TENANT))
    with pytest.raises(IdTokenError):
        await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    assert fake_idp.key_fetches == []


async def test_alg_none_and_a_wrong_key_are_refused(fake_idp):
    claims = microsoft_claims(nonce="nn", now=NOW)
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), forged({"alg": "none"}, claims), nonce="nn", now=NOW)
    assert e.value.check == "alg"
    fake_idp.tenant_keys[TENANT] = [public_jwk(issuer=f"https://login.microsoftonline.com/{TENANT}/v2.0",
                                               key_pem=make_certificate()[1])]
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), sign(claims), nonce="nn", now=NOW)
    assert e.value.check == "signature"


@pytest.mark.parametrize("key_issuer, ok", [
    (f"https://login.microsoftonline.com/{TENANT}/v2.0", True),
    ("https://login.microsoftonline.com/{tenantid}/v2.0", True),
    ("https://login.microsoftonline.com/{TenantId}/v2.0", True),
    (f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0", False),
    (None, False),
])
async def test_the_signing_key_must_belong_to_the_issuer(fake_idp, key_issuer, ok):
    extra = {} if key_issuer is None else {"issuer": key_issuer}
    fake_idp.tenant_keys[TENANT] = [public_jwk(**extra)]
    token = sign(microsoft_claims(nonce="nn", now=NOW))
    if ok:
        await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    else:
        with pytest.raises(IdTokenError) as e:
            await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
        assert e.value.check == "key_issuer"


async def test_a_missing_id_token_is_refused(fake_idp):
    with pytest.raises(IdTokenError) as e:
        await mi.validate_id_token(make_config(), None, nonce="nn", now=NOW)
    assert e.value.check == "missing"


@pytest.mark.parametrize("name, stored", [(DROP, None), ("", None), ("a" * 300, "a" * 255)])
async def test_the_sign_in_name_is_optional_and_bounded(fake_idp, name, stored):
    token = sign(microsoft_claims(nonce="nn", now=NOW, preferred_username=name))
    identity = await mi.validate_id_token(make_config(), token, nonce="nn", now=NOW)
    assert identity.sign_in_name == stored
