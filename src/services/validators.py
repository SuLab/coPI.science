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


# Leading characters that make Excel / LibreOffice / Google Sheets evaluate a
# CSV cell as a formula or DDE payload when the file is opened.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_cell(value: object) -> str:
    """Neutralize spreadsheet formula/DDE injection in an exported CSV cell.

    Untrusted text (e.g. a public waitlist name/note) written verbatim into a
    CSV becomes a live formula when an admin opens the file in a spreadsheet.
    Prefixing a leading dangerous character with a single quote forces text
    interpretation without otherwise changing the value (audit SEC-20).
    """
    text = "" if value is None else str(value)
    if text and text[0] in _CSV_INJECTION_PREFIXES:
        return "'" + text
    return text
