"""src/services/id_tokens.py (spec 2026-10-13 §5.5)."""
import time

import httpx
import pytest

from src.services import id_tokens
from src.services.id_tokens import (
    IdTokenError,
    KeySetCache,
    check_audience,
    check_times,
    unverified_parts,
    verify_signature,
)
from tests.oidc_fakes import KID, OTHER_KEY_PEM, forged, public_jwk, sign

URL = "https://keys.example/jwks"


class _Keys:
    def __init__(self):
        self.keys = [public_jwk()]
        self.status = 200
        self.calls = 0

    def handler(self, request):
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status)
        return httpx.Response(200, json={"keys": self.keys})


@pytest.fixture
def keys(monkeypatch):
    k = _Keys()
    monkeypatch.setattr(id_tokens, "HTTP_TRANSPORT", httpx.MockTransport(k.handler))
    return k


def test_unverified_parts_reads_header_and_claims():
    header, claims = unverified_parts(sign({"a": 1}))
    assert header["alg"] == "RS256" and header["kid"] == KID and claims == {"a": 1}


@pytest.mark.parametrize("alg", ["none", "HS256", "PS256", None])
def test_only_rs256_is_accepted(alg):
    with pytest.raises(IdTokenError) as e:
        unverified_parts(forged({"alg": alg}, {"a": 1}))
    assert e.value.check == "alg"


@pytest.mark.parametrize("token", ["", "a.b", "!!.!!.!!", None, 5])
def test_malformed_tokens_are_refused(token):
    with pytest.raises(IdTokenError) as e:
        unverified_parts(token)
    assert e.value.check == "malformed"


def test_verify_signature_accepts_the_right_key_and_refuses_another():
    token = sign({"a": 1})
    assert verify_signature(token, public_jwk()) == {"a": 1}
    with pytest.raises(IdTokenError) as e:
        verify_signature(token, public_jwk(key_pem=OTHER_KEY_PEM))
    assert e.value.check == "signature"


def test_check_audience():
    check_audience({"aud": "cid"}, "cid")
    check_audience({"aud": ["cid"]}, "cid")
    for claims, client_id in (({"aud": "x"}, "cid"), ({"aud": ["cid", "x"]}, "cid"),
                              ({}, "cid"), ({"aud": ""}, "")):
        with pytest.raises(IdTokenError):
            check_audience(claims, client_id)


def test_check_times_allows_the_leeway_and_no_more():
    now = 1_000_000
    check_times({"exp": now - 299, "iat": now + 299, "nbf": now + 299}, now)
    for bad in ({"exp": now - 301, "iat": now}, {"exp": now + 60, "iat": now + 301},
                {"exp": now + 60, "iat": now, "nbf": now + 301}, {"iat": now},
                {"exp": now + 60}, {"exp": True, "iat": now}, {"exp": "1", "iat": now}):
        with pytest.raises(IdTokenError):
            check_times(bad, now)


async def test_keys_are_cached_within_the_refresh_window(keys):
    cache, now = KeySetCache(), time.time()
    assert (await cache.key_for(URL, KID, now=now))["kid"] == KID
    await cache.key_for(URL, KID, now=now + id_tokens.KEY_REFRESH_SECONDS - 1)
    assert keys.calls == 1
    await cache.key_for(URL, KID, now=now + id_tokens.KEY_REFRESH_SECONDS)
    assert keys.calls == 2


async def test_an_unknown_kid_refetches_at_most_once_per_five_minutes(keys):
    cache, now = KeySetCache(), time.time()
    await cache.key_for(URL, KID, now=now)
    for t in (now + 1, now + 2):
        with pytest.raises(IdTokenError) as e:
            await cache.key_for(URL, "rotated", now=t)
        assert e.value.check == "kid"
    assert keys.calls == 2  # one forced refetch, then throttled
    keys.keys = [public_jwk("rotated")]
    with pytest.raises(IdTokenError):
        await cache.key_for(URL, "rotated", now=now + 3)
    found = await cache.key_for(URL, "rotated", now=now + 2 + id_tokens.UNKNOWN_KID_REFETCH_SECONDS)
    assert found["kid"] == "rotated"


async def test_a_failed_refresh_serves_cached_keys_within_the_ttl(keys):
    cache, now = KeySetCache(), time.time()
    await cache.key_for(URL, KID, now=now)
    keys.status = 503
    later = now + id_tokens.KEY_REFRESH_SECONDS + 10
    assert (await cache.key_for(URL, KID, now=later))["kid"] == KID
    with pytest.raises(IdTokenError) as e:
        await cache.key_for(URL, KID, now=now + id_tokens.KEY_TTL_SECONDS + 1)
    assert e.value.check == "keys"


async def test_a_failed_refresh_is_not_retried_on_every_request(keys):
    cache, now = KeySetCache(), time.time()
    await cache.key_for(URL, KID, now=now)
    keys.status = 503
    stale = now + id_tokens.KEY_REFRESH_SECONDS
    await cache.key_for(URL, KID, now=stale)
    await cache.key_for(URL, KID, now=stale + 1)
    with pytest.raises(IdTokenError):  # an unknown kid makes no forced fetch during the backoff
        await cache.key_for(URL, "rotated", now=stale + 2)
    assert keys.calls == 2
    await cache.key_for(URL, KID, now=stale + id_tokens.FAILED_REFRESH_RETRY_SECONDS)
    assert keys.calls == 3


async def test_a_cold_cache_with_the_endpoint_down_fails_closed(keys):
    keys.status = 500
    cache, now = KeySetCache(), time.time()
    for t in (now, now + 1):  # the second sign-in fails at once instead of waiting out a fetch
        with pytest.raises(IdTokenError) as e:
            await cache.key_for(URL, KID, now=t)
        assert e.value.check == "keys"
    assert keys.calls == 1
    keys.status = 200
    assert (await cache.key_for(URL, KID, now=now + id_tokens.FAILED_REFRESH_RETRY_SECONDS))["kid"] == KID
    assert keys.calls == 2


async def test_a_token_without_kid_uses_a_one_key_set_only(keys):
    cache, now = KeySetCache(), time.time()
    assert (await cache.key_for(URL, None, now=now))["kid"] == KID
    keys.keys = [public_jwk("a"), public_jwk("b")]
    with pytest.raises(IdTokenError):
        await KeySetCache().key_for(URL, None, now=now)
