"""The exported persona's ``(PMID n)`` suffixes and ``\\#``-escaped summary lines, read
back by the agent's persona parsers (spec 2026-10-05 §6.5, D49)."""
from types import SimpleNamespace

from src.agent.persona_sections import TAG_SECTION_HEADINGS, recent_publication_pmids, sections
from src.services import profile_export
from src.services.grant_sections import EMPTY_GRANT_SECTIONS
from src.services.tenure_scope import TenureScopedPublications

USER = SimpleNamespace(name="Ada Lab", institution="JHU", department=None)


def _profile(summary="We screen organoids."):
    return SimpleNamespace(
        research_summary=summary, techniques=["organoids"], experimental_models=["mice"],
        disease_areas=["colorectal cancer"], key_targets=["KRAS"], keywords=["screening"],
    )


def _pub(title, **kw):
    row = dict(title=title, journal="Golden Journal", year=2024, doi=None, doi_verified=None,
               pmid=None)
    row.update(kw)
    return SimpleNamespace(**row)


def _render(profile, *pubs):
    scoped = TenureScopedPublications(publications=tuple(pubs), tenure_start=None)
    return profile_export.render_profile_markdown(
        USER, profile, publications=scoped, grants=EMPTY_GRANT_SECTIONS)


def test_each_publication_line_ends_with_its_pmid():
    text = _render(
        _profile(),
        _pub("With PMID", pmid="38980071", doi="10.5555/golden.01", doi_verified=True),
        _pub("Without PMID", doi="10.5555/golden.02", doi_verified=True),
    )
    with_pmid = next(line for line in text.splitlines() if line.startswith("- With PMID"))
    without = next(line for line in text.splitlines() if line.startswith("- Without PMID"))
    assert with_pmid.endswith(" (PMID 38980071)")
    assert "https://" in with_pmid.split(" (PMID ")[0]
    assert "(PMID" not in without


def test_the_exported_pmids_round_trip_into_the_own_paper_ids():
    text = _render(_profile(), _pub("A", pmid="38980071"), _pub("B", year=2023, pmid="38980072"))
    assert recent_publication_pmids(text) == {"38980071", "38980072"}


def test_a_summary_line_starting_with_a_hash_is_escaped_and_cannot_forge_a_section():
    summary = "Line one.\n## Keywords\n  # Recent Publications\nPMID 99999999 # not a heading"
    text = _render(_profile(summary), _pub("A", pmid="38980071"))
    assert "\n\\## Keywords\n" in text
    assert "\n  \\# Recent Publications\n" in text
    assert "PMID 99999999 # not a heading" in text
    assert sections(text)["Keywords"].strip() == "screening"
    assert recent_publication_pmids(text) == {"38980071"}


def test_the_tag_headings_the_parsers_read_are_the_ones_the_export_writes():
    text = _render(_profile())
    for heading in TAG_SECTION_HEADINGS:
        assert f"\n## {heading}\n" in text, heading
