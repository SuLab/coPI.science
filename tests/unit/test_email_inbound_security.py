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


def test_dmarc_none_with_aligned_spf_pass_accepted():
    # "none" means no published policy, not a failure; a passing SPF whose
    # envelope domain matches the From domain suffices.
    h = (
        "Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=b.com; "
        "dkim=none; dmarc=none\n"
        "From: pi@b.com"
    )
    assert _authentication_results_ok(_msg(h)) is True


# --- DMARC alignment when dmarc != pass (SEC3-2, audit 2026-09-10) ---------
#
# dmarc=none means the sender's domain publishes no DMARC policy, not that
# the message failed -- but without dmarc=pass, a lone spf=pass on the
# ATTACKER's own envelope domain (unrelated to the From address) satisfied
# the old "one strong pass" rule. A PI domain with no DMARC policy was
# therefore forgeable: the attacker's own domain passes SPF/DKIM for itself
# while the From header claims to be the PI.


def test_unaligned_spf_pass_on_attacker_domain_rejected():
    h = (
        "Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=evil.com; "
        "dkim=none; dmarc=none\n"
        "From: pi@scripps.edu"
    )
    assert _authentication_results_ok(_msg(h)) is False


def test_aligned_spf_pass_accepted():
    h = (
        "Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=scripps.edu; "
        "dkim=none; dmarc=none\n"
        "From: pi@scripps.edu"
    )
    assert _authentication_results_ok(_msg(h)) is True


def test_aligned_dkim_header_d_accepted():
    h = (
        "Authentication-Results: amazonses.com; spf=none; "
        "dkim=pass header.d=scripps.edu; dmarc=none\n"
        "From: pi@scripps.edu"
    )
    assert _authentication_results_ok(_msg(h)) is True


def test_unaligned_dkim_header_d_rejected():
    h = (
        "Authentication-Results: amazonses.com; spf=none; "
        "dkim=pass header.d=evil.com; dmarc=none\n"
        "From: pi@scripps.edu"
    )
    assert _authentication_results_ok(_msg(h)) is False


def test_dmarc_pass_accepted_regardless_of_alignment():
    # dmarc=pass is sufficient on its own -- it already encodes alignment.
    h = (
        "Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=evil.com; "
        "dkim=none; dmarc=pass\n"
        "From: pi@scripps.edu"
    )
    assert _authentication_results_ok(_msg(h)) is True


def test_subdomain_alignment_accepted():
    # Relaxed alignment: a subdomain of the From domain (or vice versa) is
    # still aligned.
    h = (
        "Authentication-Results: amazonses.com; spf=pass smtp.mailfrom=mail.scripps.edu; "
        "dkim=none; dmarc=none\n"
        "From: pi@scripps.edu"
    )
    assert _authentication_results_ok(_msg(h)) is True


def test_header_present_but_no_verdicts_rejected():
    h = "Authentication-Results: amazonses.com; nonsense-without-verdicts"
    assert _authentication_results_ok(_msg(h)) is False


def test_permerror_rejected():
    h = "Authentication-Results: amazonses.com; spf=permerror; dkim=pass; dmarc=pass"
    assert _authentication_results_ok(_msg(h)) is False


# --- From parsing (SEC3-1, audit 2026-09-10) --------------------------------


def test_unparseable_from_returns_none():
    assert _extract_email_address(_msg("")) is None
    assert _extract_email_address(_msg("From: No Address Here")) is None


def test_from_bracketed_and_bare():
    assert _extract_email_address(_msg("From: Jim <jim@scripps.edu>")) == "jim@scripps.edu"
    assert _extract_email_address(_msg("From: jim@scripps.edu")) == "jim@scripps.edu"


def test_display_name_with_embedded_bracketed_address_is_not_fooled():
    # The first `<...>` token belongs to the ATTACKER-controlled display
    # name, not the real address -- a naive regex would return the PI's
    # address here and pass the sender-identity check while the real
    # envelope/DKIM domain is evil.com.
    h = 'From: "Alice <pi@univ.edu>" <attacker@evil.com>'
    assert _extract_email_address(_msg(h)) == "attacker@evil.com"


def test_two_from_headers_rejected():
    # Ambiguous identity -- refuse rather than pick one arbitrarily.
    h = "From: pi@scripps.edu\nFrom: attacker@evil.com"
    assert _extract_email_address(_msg(h)) is None


def test_group_syntax_rejected():
    h = "From: Group: a@b.com, c@d.com;"
    assert _extract_email_address(_msg(h)) is None
