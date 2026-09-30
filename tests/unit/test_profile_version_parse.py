"""The hidden profile_version field is parsed strictly: anything but a plain
in-range number means "no version", never a 500."""
import pytest

from src.services.profile_edit import parse_expected_version


@pytest.mark.parametrize("raw, expected", [
    ("3", 3), (" 12 ", 12), ("", None), (None, None), ("abc", None),
    ("²", None), ("99999999999", None), ("-1", None), ("1.5", None),
])
def test_parse_expected_version(raw, expected):
    assert parse_expected_version(raw) == expected
