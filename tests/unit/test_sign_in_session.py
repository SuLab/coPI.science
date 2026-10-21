"""The sign-in stamp and the freshness test that linking a Microsoft account needs."""

import pytest

from src.sign_in_session import (
    LINK_REAUTH_MAX_AGE_SECONDS,
    ORCID_REAUTH_AT_KEY,
    SIGNED_IN_AT_KEY,
    SIGNED_IN_VIA_KEY,
    VIA_MICROSOFT,
    VIA_ORCID,
    fresh_orcid_sign_in,
    record_orcid_reauth,
    record_sign_in,
)

NOW = 1_000_000


def _session(**kw):
    return {SIGNED_IN_VIA_KEY: VIA_ORCID, ORCID_REAUTH_AT_KEY: NOW - 10, **kw}


def test_record_sign_in_stamps_the_provider_and_time():
    session = {}
    record_sign_in(session, VIA_MICROSOFT, now=NOW)
    assert session == {SIGNED_IN_AT_KEY: NOW, SIGNED_IN_VIA_KEY: VIA_MICROSOFT}


def test_record_orcid_reauth_stamps_the_time():
    session = {}
    record_orcid_reauth(session, now=NOW)
    assert session == {ORCID_REAUTH_AT_KEY: NOW}


def test_a_recent_proven_orcid_reauth_is_fresh():
    assert fresh_orcid_sign_in(_session(), now=NOW) is True


def test_a_session_with_no_stamp_is_stale():
    assert fresh_orcid_sign_in({}, now=NOW) is False
    assert fresh_orcid_sign_in({SIGNED_IN_VIA_KEY: VIA_ORCID}, now=NOW) is False


def test_a_microsoft_sign_in_is_never_fresh():
    assert fresh_orcid_sign_in(_session(**{SIGNED_IN_VIA_KEY: VIA_MICROSOFT}), now=NOW) is False


@pytest.mark.parametrize("value", [True, "1000", 1.5, None])
def test_a_malformed_reauth_stamp_is_stale(value):
    assert fresh_orcid_sign_in(_session(**{ORCID_REAUTH_AT_KEY: value}), now=NOW) is False


def test_the_window_is_zero_to_900_seconds_inclusive():
    assert LINK_REAUTH_MAX_AGE_SECONDS == 900
    def at(age):
        return _session(**{ORCID_REAUTH_AT_KEY: NOW - age})

    assert fresh_orcid_sign_in(at(0), now=NOW) is True
    assert fresh_orcid_sign_in(at(900), now=NOW) is True
    assert fresh_orcid_sign_in(at(901), now=NOW) is False


def test_a_future_reauth_stamp_is_refused():
    assert fresh_orcid_sign_in(_session(**{ORCID_REAUTH_AT_KEY: NOW + 1}), now=NOW) is False
