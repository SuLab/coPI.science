"""Tests for the shared validators (SEC-16 ReDoS cap, SEC-20 CSV safety)."""

import time

from src.services.validators import (
    MAX_EMAIL_LENGTH,
    csv_safe_cell,
    is_valid_email,
)


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


def test_length_boundary_is_254_inclusive():
    # RFC 5321 caps an address at exactly 254 chars (SEC-16). Hard-code the boundary
    # here — do NOT derive the lengths from MAX_EMAIL_LENGTH — so this pins the actual
    # value: a bumped constant or a `>` -> `>=` slip is caught (mutation-tested).
    suffix = "@example.com"
    at_254 = "a" * (254 - len(suffix)) + suffix
    at_255 = "a" * (255 - len(suffix)) + suffix
    assert len(at_254) == 254
    assert len(at_255) == 255
    assert is_valid_email(at_254) is True   # exactly at the cap is accepted
    assert is_valid_email(at_255) is False  # one over is rejected
    assert MAX_EMAIL_LENGTH == 254


def test_length_cap_prevents_redos():
    # A dot-heavy payload that makes the underlying regex backtrack
    # superlinearly. With the length cap it must return promptly regardless.
    payload = ("a" * 40 + ".") * 4000 + "!"
    start = time.monotonic()
    assert is_valid_email(payload) is False
    assert time.monotonic() - start < 0.5


def test_csv_safe_neutralizes_formula_prefixes():
    assert csv_safe_cell("=1+1") == "'=1+1"
    assert csv_safe_cell("+cmd") == "'+cmd"
    assert csv_safe_cell("-2+3") == "'-2+3"
    assert csv_safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert csv_safe_cell("\t=1") == "'\t=1"
    assert csv_safe_cell("\rfoo") == "'\rfoo"


def test_csv_safe_leaves_normal_values():
    assert csv_safe_cell("alice@x.com".lstrip("@")) == "alice@x.com".lstrip("@")
    assert csv_safe_cell("Jane Doe") == "Jane Doe"
    assert csv_safe_cell("Scripps Research") == "Scripps Research"
    assert csv_safe_cell("") == ""
    assert csv_safe_cell(None) == ""
