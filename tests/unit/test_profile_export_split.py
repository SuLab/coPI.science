"""export_profile_to_markdown / render_profile_markdown boundaries. The byte-level oracle
is the six test_export_via_*_gm golden entries (spec 2026-10-05 §8)."""
from types import SimpleNamespace

import pytest

from src.services import profile_export
from src.services.grant_sections import EMPTY_GRANT_SECTIONS, GrantLine, GrantSections
from src.services.tenure_scope import TenureScopedPublications

USER = SimpleNamespace(name="Ada Lab", institution="JHU", department="Bio")
PROFILE = SimpleNamespace(research_summary="Sum", techniques=["a"], experimental_models=None,
                          disease_areas=[], key_targets=None, keywords=["x"],
                          grant_titles=["NEVER RENDERED"])
PUBS = TenureScopedPublications(
    publications=(SimpleNamespace(title="P.", journal="J", year=2024, doi=None, pmid="1"),),
    tenure_start=None)
SECTIONS = GrantSections(
    active=(GrantLine("nih_reporter", "R01A", "Live", "NIH R01", 2019, 2027),),
    past=(GrantLine("orcid", "orcid:k", "Old", "Golden Foundation", 2011, 2013),),
    tenure_start=2010)


def test_empty_agent_id_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    assert profile_export.export_profile_to_markdown(
        USER, PROFILE, "", grants=EMPTY_GRANT_SECTIONS) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", [None, [], ["Live"]])
def test_grants_must_be_grant_sections(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    with pytest.raises(TypeError, match="GrantSections"):
        profile_export.export_profile_to_markdown(USER, PROFILE, "lab1", publications=PUBS,
                                                  grants=bad)
    with pytest.raises(TypeError, match="GrantSections"):
        profile_export.render_profile_markdown(USER, PROFILE, publications=PUBS, grants=bad)
    with pytest.raises(TypeError):
        profile_export.export_profile_to_markdown(USER, PROFILE, "lab1", publications=PUBS)


def test_bare_list_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    with pytest.raises(TypeError, match="scoped_publications_for_export"):
        profile_export.export_profile_to_markdown(USER, PROFILE, "lab1", publications=[],
                                                  grants=EMPTY_GRANT_SECTIONS)


def test_sections_follow_recent_publications_and_grant_titles_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    path = profile_export.export_profile_to_markdown(USER, PROFILE, "lab1", publications=PUBS,
                                                     grants=SECTIONS)
    text = path.read_text()
    assert text == profile_export.render_profile_markdown(USER, PROFILE, publications=PUBS,
                                                          grants=SECTIONS)
    assert "NEVER RENDERED" not in text
    assert (text.index("## Recent Publications") < text.index("## Active Grants")
            < text.index("## Past Grants (since 2010)"))
    assert text.endswith("- Old (Golden Foundation, 2011–2013)\n")
