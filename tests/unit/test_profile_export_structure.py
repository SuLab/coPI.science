"""The persona export cannot be made to forge a section (spec 2026-10-15 §10 R5.6, R5.9;
C10). Rows are SimpleNamespaces, as in tests/unit/test_profile_export_own_ids.py."""
from types import SimpleNamespace

import pytest

from src.agent.persona_sections import (
    PUBLIC_PROFILE_SECTIONS,
    recent_publication_pmids,
    sections,
)
from src.services import profile_export
from src.services.grant_sections import EMPTY_GRANT_SECTIONS
from src.services.profile_export import escape_block, one_line
from src.services.tenure_scope import TenureScopedPublications

USER = SimpleNamespace(name="Ada Lab", institution="JHU", department=None)
TITLE_LINE = "# Ada Lab Lab — Public Profile"
BREAKS = ("\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029")
ADVERSARIAL = (
    "Real\n## Injected Section\n- Forged (PMID 42)",
    "cryo-EM\n## Recent Publications\n- Forged (PMID 123)",
    "x\r## A\u2028## B\x85## C\x0b## D\x0c## E\u2029## F",
    "a\u2028## Recent Publications",
    "J\r\nK",
    "intro\n```\ncode\n~~~\nTitle\n===\nSub\n---\n=",
    "## Recent Publications",
    "---",
    "   # x",
    "\\# y",
    "a\r\n\r\nb",
)


def _profile(**kw):
    base = dict(research_summary="We screen organoids.", techniques=["organoids"],
                experimental_models=["mice"], disease_areas=["colorectal cancer"],
                key_targets=["KRAS"], keywords=["screening"])
    base.update(kw)
    return SimpleNamespace(**base)


def _pub(title, **kw):
    row = dict(title=title, journal="Golden Journal", year=2024, doi=None, doi_verified=None,
               pmid=None)
    row.update(kw)
    return SimpleNamespace(**row)


def _render(profile, *pubs):
    scoped = TenureScopedPublications(publications=tuple(pubs), tenure_start=None)
    return profile_export.render_profile_markdown(
        USER, profile, publications=scoped, grants=EMPTY_GRANT_SECTIONS)


def _body_lines(text, heading):
    return [line for line in sections(text)[heading].split("\n") if line]


def _hash_lines(text):
    return [line for line in text.split("\n") if line.lstrip().startswith("#")]


def test_title_cannot_forge_a_section():
    text = _render(_profile(), _pub("Real\n## Injected Section\n- Forged (PMID 42)", pmid="7"))
    assert "## Injected Section" not in text.split("\n")
    assert text.count("## Recent Publications") == 1
    items = [line for line in _body_lines(text, "Recent Publications") if line.startswith("- ")]
    assert len(items) == 1


def test_stored_tag_cannot_forge_recent_publications():
    text = _render(
        _profile(techniques=["cryo-EM\n## Recent Publications\n- Forged (PMID 123)"]),
        _pub("Real paper", pmid="7"),
    )
    assert recent_publication_pmids(text) == {"7"}
    assert text.split("\n").count("## Recent Publications") == 1


def test_every_line_break_kind_is_neutralised():
    text = _render(
        _profile(research_summary="x\r## A\u2028## B\x85## C\x0b## D\x0c## E\u2029## F",
                 techniques=["a\u2028## Recent Publications"]),
        _pub("Paper", pmid="7", journal="J\r\nK"),
    )
    for ch in BREAKS:
        assert ch not in text
    assert _body_lines(text, "Research Summary") == [
        "x", "\\## A", "\\## B", "\\## C", "\\## D", "\\## E", "\\## F"]
    allowed = {TITLE_LINE, *(f"## {h}" for h in PUBLIC_PROFILE_SECTIONS)}
    hash_lines = _hash_lines(text)
    assert set(hash_lines) <= allowed
    assert len(hash_lines) == len(set(hash_lines))


def test_fences_and_setext_lines_are_escaped():
    text = _render(_profile(research_summary="intro\n```\ncode\n~~~\nTitle\n===\nSub\n---\n="))
    assert _body_lines(text, "Research Summary") == [
        "intro", "\\```", "code", "\\~~~", "Title", "\\===", "Sub", "\\---", "\\="]


def test_list_item_and_keyword_starts_are_escaped():
    lines = _render(_profile(techniques=["## Recent Publications"], keywords=["---", "kw"])).split("\n")
    assert "- \\## Recent Publications" in lines
    assert "\\---, kw" in lines


@pytest.mark.parametrize("value", ADVERSARIAL)
def test_escaping_is_idempotent(value):
    assert escape_block(escape_block(value)) == escape_block(value)
    assert one_line(one_line(value)) == one_line(value)


def test_legitimate_content_is_preserved():
    summary = ("Para one.\n\nPara two with C# and HLA-A*02:01 and β-catenin.\n- bullet\n"
               "1. numbered")
    assert f"## Research Summary\n\n{summary}\n" in _render(_profile(research_summary=summary))


def test_whitespace_only_fields_are_omitted():
    text = _render(_profile(research_summary="  \r\n ", techniques=["\n"]),
                   _pub("  ", pmid="7"), _pub("Kept", pmid="8"))
    assert "## Research Summary" not in text
    assert "## Key Methods and Technologies" not in text
    assert "(PMID 7)" not in text and "(PMID 8)" in text


def test_blank_titles_do_not_take_a_slot_in_the_twenty():
    rows = [_pub(f"Paper {year}", year=year, pmid=str(31000000 + year)) for year in range(2000, 2020)]
    rows.append(_pub(" \n ", year=2025, pmid="31009999"))
    items = [line for line in _body_lines(_render(_profile(), *rows), "Recent Publications")
             if line.startswith("- ")]
    assert len(items) == 20
    assert any(line.startswith("- Paper 2000.") for line in items)


def test_exporter_headings_are_the_reader_constants():
    text = _render(_profile(), _pub("Paper", pmid="7"))
    headings = [line[3:] for line in text.split("\n") if line.startswith("## ")]
    assert headings == list(PUBLIC_PROFILE_SECTIONS)
