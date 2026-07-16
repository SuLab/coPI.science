"""Shared validators for user-supplied input fields."""

import re

# Local-part@domain-with-a-dot. Deliberately permissive: this only rejects the
# obviously malformed — real deliverability is not our concern here. Always go
# through :func:`is_valid_email` so the length cap below is applied first.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# RFC 5321 caps an email address at 254 characters. Enforcing this *before*
# running the regex is essential, not cosmetic: the pattern backtracks
# superlinearly on long dot-heavy input, so matching uncapped attacker input on
# an unauthenticated field (waitlist / onboarding / profile) is a ReDoS vector
# (audit SEC-16).
MAX_EMAIL_LENGTH = 254


def is_valid_email(value: str | None) -> bool:
    """True if ``value`` looks like an email address and is within RFC length.

    The length cap is checked first so the regex never runs on oversized input.
    """
    if not value or len(value) > MAX_EMAIL_LENGTH:
        return False
    return _EMAIL_RE.match(value) is not None
