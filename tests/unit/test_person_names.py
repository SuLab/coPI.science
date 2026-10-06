"""The shared name parser (spec 2026-10-05 §4.1, D22, D60)."""
import pytest

from src.services import agent_identity
from src.services.company_sources import pi_name
from src.services.person_names import (
    InvalidPersonName,
    name_from_machine_source,
    parse_person_name,
    sanitize_person_name,
    surname_candidates,
    validate_person_name,
)

SLUG_CASES = [
    "Anna Müller", "José García López", "Søren Kierkegaard", "Lars Ørsted", "Liam O'Brien",
    "Ana Hamacher-Brady", "John Smith Jr.", "Mary Jones III", "Jean-Luc Picard PhD", "Naoki Ii",
    "Henry Ford II", "0000-0002-1825-009X", "王小明", "Иван Петров", "1234 5678",
    "Ann Wolfeschlegelsteinhausenbergerdorff", "Dr. Victor Velculescu", "Jane Doe, PhD",
]


@pytest.mark.parametrize("name", SLUG_CASES)
def test_slug_surname_is_agent_identity_surname(name):
    assert parse_person_name(name).slug_surname == agent_identity._surname(name)


@pytest.mark.parametrize(("name", "slug"), [
    ("John Smith Jr.", "Smith"), ("Henry Ford II", "Ford"), ("Naoki Ii", "Ii"),
    ("Jean-Luc Picard PhD", "Picard"), ("Anna Müller", "Muller"),
])
def test_slug_surnames_do_not_change(name, slug):
    assert parse_person_name(name).slug_surname == slug


@pytest.mark.parametrize(("name", "first", "display"), [
    ("Victor E. Velculescu", "Victor", "Velculescu"),
    ("Dr. Victor Velculescu", "Victor", "Velculescu"),
    ("Iris van 't Erve", "Iris", "van 't Erve"),
    ("Anne Hamacher-Brady", "Anne", "Hamacher-Brady"),
])
def test_display_surname_matches_pi_name(name, first, display):
    parsed = parse_person_name(name)
    assert (parsed.first, parsed.display_surname) == (first, display)
    assert pi_name(name).surname == display and pi_name(name).surname_keys == parsed.surname_keys


@pytest.mark.parametrize(("name", "expected"), [
    ("John Smith Jr.", {"smith"}), ("Mary Jones III", {"jones"}), ("Jane Doe, PhD", {"doe"}),
    ("Jane Doe, M.D., Ph.D.", {"doe"}), ("Picard PhD Jean", {"jean", "phdjean"}),
])
def test_suffixes_and_comma_degrees_are_not_surnames(name, expected):
    assert parse_person_name(name).surname_keys == frozenset(expected)


def test_particles_multi_token_and_german_folding():
    assert surname_candidates("Iris van 't Erve") == ("Erve", "van 't Erve", "'t Erve")
    garcia = parse_person_name("Gabriel García Márquez")
    assert {"marquez", "garciamarquez"} <= garcia.surname_keys
    assert {"muller", "mueller"} <= parse_person_name("Anna Müller").surname_keys


@pytest.mark.parametrize("name", ["0000-0002-1825-009X", "1234 5678"])
def test_orcid_id_and_digit_only_names_are_flagged_and_match_nothing(name):
    parsed = parse_person_name(name)
    assert parsed.is_orcid_id and parsed.surname_keys == frozenset()
    assert surname_candidates(name) == ()


def test_a_real_name_is_not_an_orcid_id():
    assert parse_person_name("Victor Velculescu").is_orcid_id is False


@pytest.mark.parametrize(
    "name", ["Zoë O’Neil-Smith", "Ngũgĩ wa Thiong'o", "王小明", "J. R. R. Tolkien"]
)
def test_validate_accepts_the_allowlist(name):
    assert validate_person_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "Robert'); DROP TABLE users;--", "Line\nBreak", "A" * 101, "1234"]
)
def test_validate_refuses_outside_the_allowlist(name):
    with pytest.raises(InvalidPersonName):
        validate_person_name(name)


def test_validate_checks_only_a_changed_name():
    orcid = "0000-0002-1825-009X"
    assert validate_person_name(orcid, previous=orcid) == orcid


def test_sanitize_strips_and_flags_and_refuses_ids():
    cleaned = sanitize_person_name("Jane <b>Doe</b>")
    assert cleaned is not None and cleaned.changed and "<" not in cleaned.name
    assert sanitize_person_name("Jane Doe").changed is False
    assert sanitize_person_name("0000-0002-1825-009X") is None
    assert sanitize_person_name("1234 5678") is None


def test_machine_names_keep_todays_fallback_for_ids():
    assert name_from_machine_source("Jane Doe™") == ("Jane Doe", True)
    assert name_from_machine_source("Jane Doe") == ("Jane Doe", False)
    assert name_from_machine_source("0000-0002-1825-009X") == ("0000-0002-1825-009X", False)
