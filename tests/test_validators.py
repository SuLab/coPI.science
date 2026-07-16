"""Tests for the shared email validator (SEC-16 ReDoS length cap)."""

import time

from src.services.validators import MAX_EMAIL_LENGTH, is_valid_email


def test_accepts_normal_addresses():
    assert is_valid_email("alice@example.com")
    assert is_valid_email("a.b+tag@sub.domain.edu")


def test_rejects_malformed():
    assert not is_valid_email("")
    assert not is_valid_email(None)
    assert not is_valid_email("no-at-sign")
    assert not is_valid_email("no@dot")
    assert not is_valid_email("has space@example.com")


def test_rejects_over_length():
    over = "a" * (MAX_EMAIL_LENGTH - 10) + "@example.com"
    assert len(over) > MAX_EMAIL_LENGTH
    assert not is_valid_email(over)


def test_length_cap_prevents_redos():
    # A dot-heavy payload that makes the underlying regex backtrack
    # superlinearly. With the length cap it must return promptly regardless.
    payload = ("a" * 40 + ".") * 4000 + "!"
    start = time.monotonic()
    assert is_valid_email(payload) is False
    assert time.monotonic() - start < 0.5
