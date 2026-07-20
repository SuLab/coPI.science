"""SEC-5: anti-spoofing checks for inbound email processing."""

import email

from src.services.email_inbound import (
    _authentication_results_ok,
    _extract_email_address,
)


def _msg(headers: str) -> email.message.Message:
    return email.message_from_string(headers + "\n\nbody text")


# --- Authentication-Results gate ------------------------------------------


def test_missing_auth_header_rejected():
    # No Authentication-Results => not delivered via our SES path => reject.
    assert _authentication_results_ok(_msg("From: a@b.com")) is False


def test_all_pass_accepted():
    h = ("Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=b.com; "
         "dkim=pass header.d=b.com; dmarc=pass header.from=b.com")
    assert _authentication_results_ok(_msg(h)) is True


def test_dmarc_fail_rejected():
    h = "Authentication-Results: amazonses.com; spf=pass; dkim=pass; dmarc=fail"
    assert _authentication_results_ok(_msg(h)) is False


def test_spf_softfail_rejected():
    h = "Authentication-Results: amazonses.com; spf=softfail; dkim=fail; dmarc=fail"
    assert _authentication_results_ok(_msg(h)) is False


def test_dmarc_none_with_spf_pass_accepted():
    # "none" means no published policy, not a failure; a passing SPF suffices.
    h = "Authentication-Results: amazonses.com; spf=pass; dkim=none; dmarc=none"
    assert _authentication_results_ok(_msg(h)) is True


def test_header_present_but_no_verdicts_rejected():
    h = "Authentication-Results: amazonses.com; nonsense-without-verdicts"
    assert _authentication_results_ok(_msg(h)) is False


def test_permerror_rejected():
    h = "Authentication-Results: amazonses.com; spf=permerror; dkim=pass; dmarc=pass"
    assert _authentication_results_ok(_msg(h)) is False


# --- From parsing ----------------------------------------------------------


def test_unparseable_from_returns_none():
    assert _extract_email_address("") is None
    assert _extract_email_address("No Address Here") is None


def test_from_bracketed_and_bare():
    assert _extract_email_address("Jim <jim@scripps.edu>") == "jim@scripps.edu"
    assert _extract_email_address("jim@scripps.edu") == "jim@scripps.edu"
