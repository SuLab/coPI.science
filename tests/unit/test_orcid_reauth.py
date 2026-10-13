"""src/services/orcid_reauth.py (spec 2026-10-13 §5.5.1, D10)."""
import time

import pytest

from src.services.id_tokens import IdTokenError
from src.services.orcid_reauth import validate_reauth_id_token
from tests.oidc_fakes import DROP, sign

NOW = int(time.time())
ORCID = "0000-0002-1825-0097"


def _claims(**overrides):
    claims = {"iss": "https://orcid.org", "aud": "APP-TEST", "sub": ORCID, "nonce": "rn",
              "iat": NOW, "exp": NOW + 600, "auth_time": NOW - 5}
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not DROP}


async def _validate(token, **kw):
    args = dict(client_id="APP-TEST", nonce="rn", orcid=ORCID, now=NOW)
    args.update(kw)
    await validate_reauth_id_token(token, **args)


async def test_a_fresh_reauth_passes(fake_idp):
    await _validate(sign(_claims()))
    await _validate(sign(_claims(), kid=None))  # ORCID publishes one key


@pytest.mark.parametrize("overrides, check", [
    ({"auth_time": NOW - 301}, "auth_time"),
    ({"auth_time": DROP}, "auth_time"),
    ({"auth_time": True}, "auth_time"),
    ({"sub": "0000-0000-0000-0001"}, "sub"),
    ({"nonce": "other"}, "nonce"),
    ({"iss": "https://sandbox.orcid.org"}, "iss"),
    ({"aud": "APP-OTHER"}, "aud"),
    ({"exp": NOW - 301}, "exp"),
])
async def test_each_failed_check_refuses(fake_idp, overrides, check):
    with pytest.raises(IdTokenError) as e:
        await _validate(sign(_claims(**overrides)))
    assert e.value.check == check


async def test_a_missing_token_or_nonce_refuses(fake_idp):
    with pytest.raises(IdTokenError):
        await _validate(None)
    with pytest.raises(IdTokenError):
        await _validate(sign(_claims()), nonce=None)
