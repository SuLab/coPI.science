"""`normalize_bullets`: the hub's own `strengths`/`risks` sidecar lists.

A malformed narrative field never costs the verdict (A20) — anything that is
not a clean, non-empty list of non-blank strings normalizes to None, and
`raw_verdict` keeps the original elsewhere.
"""
from __future__ import annotations

from src.services.assessment_detail import normalize_bullets


def test_a_list_of_strings_is_returned_stripped():
    assert normalize_bullets([" a claim ", "another claim"]) == ["a claim", "another claim"]


def test_an_empty_list_is_none():
    assert normalize_bullets([]) is None


def test_a_list_with_a_non_string_is_none():
    assert normalize_bullets(["fine", 7]) is None


def test_a_list_with_a_blank_string_is_none():
    assert normalize_bullets(["fine", "   "]) is None


def test_a_dict_is_none():
    assert normalize_bullets({"a": "b"}) is None


def test_a_string_is_none():
    assert normalize_bullets("not a list") is None


def test_none_is_none():
    assert normalize_bullets(None) is None
