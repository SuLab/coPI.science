"""Pure parts of scripts/grants_remediation.py."""
from types import SimpleNamespace as NS

import scripts.grants_remediation as gr
from src.services.person_names import parse_person_name

PERSONA = """# Jane Wang Lab — Public Profile

## Recent Publications

- A paper. *J*. (2024).

## Active Grants

- Organoid (Screening) Platform (NIH R01, 2021–2027)
- Foundation award (National Institutes of Health, NCI, 2024–)

## Past Grants (since 2010)

- Old (NIH R21, 2011–2013)
- Undated (Golden Foundation)
"""


def test_persona_grant_lines_are_parsed_by_section():
    lines = gr.parse_persona_grants(PERSONA)
    assert [(x.section, x.title, x.label, x.start, x.end) for x in lines] == [
        ("active", "Organoid (Screening) Platform", "NIH R01", 2021, 2027),
        ("active", "Foundation award", "National Institutes of Health, NCI", 2024, None),
        ("past", "Old", "NIH R21", 2011, 2013),
        ("past", "Undated", "Golden Foundation", None, None),
    ]


def test_publications_are_not_grant_lines():
    assert all(x.title != "A paper. *J*." for x in gr.parse_persona_grants(PERSONA))


def test_pre_phase_one_bare_bullets_parse_with_an_empty_label():
    old = "## Active Grants\n\n- Golden   Grant One\n- Golden Grant Two\n"
    assert [(x.section, x.title, x.label) for x in gr.parse_persona_grants(old)] == [
        ("active", "Golden Grant One", ""), ("active", "Golden Grant Two", "")]


def test_shared_awards_must_name_distinct_owned_profiles():
    keys = {"z": parse_person_name("Fidel Zavala").surname_keys,
            "c": parse_person_name("Isabelle Coppens").surname_keys}
    good = {
        "z": {"R01X": NS(reporter_profile_id=1,
                         identity_evidence={"name_on_award": "Fidel Zavala"})},
        "c": {"R01X": NS(reporter_profile_id=2,
                         identity_evidence={"name_on_award": "Isabelle Coppens"})},
    }
    assert gr.check_shared_awards(good, {"z": {1}, "c": {2}}, keys) == []
    bad = {
        "z": good["z"],
        "c": {"R01X": NS(reporter_profile_id=1,
                         identity_evidence={"name_on_award": "Fidel Zavala"})},
    }
    assert gr.check_shared_awards(bad, {"z": {1}, "c": {1}}, keys)
