"""URL -> "cited paper" rewriting for assessment prose (spec 2026-09-21 §7).

Two functions, one per render path. The plain one returns ``Markup`` and is the
only place in this change that produces HTML from a string, so its escaping is
tested harder than anything else here: the ONE ordering defect this module
exists to avoid is escape-then-match, which turns ``&`` into ``&amp;`` before
the URL matcher runs and leaves ``&amp;amp;`` in the href — a dead link.

No ``pytestmark``: these are pure functions, they need neither Postgres nor
asyncio, and this repo registers no ``unit`` marker (see ``pyproject.toml``).
"""

from __future__ import annotations

import pytest
from markupsafe import Markup, escape

from src.services.prose_citations import (
    CITATION_LABEL,
    markdown_with_citation_links,
    plain_with_citation_links,
)

DOI = "https://doi.org/10.1021/acsmedchemlett.5c00623"
DOI2 = "https://doi.org/10.64898/2026.06.29.735215"


# --- both functions, the total contract ------------------------------------


@pytest.mark.parametrize("fn", [markdown_with_citation_links, plain_with_citation_links])
def test_none_in_none_out(fn):
    assert fn(None) is None


@pytest.mark.parametrize("fn", [markdown_with_citation_links, plain_with_citation_links])
def test_empty_string_survives(fn):
    assert fn("") == ""


def test_markdown_without_a_url_is_returned_unchanged():
    text = "Three sentences of plain prose, no citation at all."
    assert markdown_with_citation_links(text) == text


def test_plain_without_a_url_is_escaped_and_nothing_else():
    assert plain_with_citation_links("a < b & c") == Markup("a &lt; b &amp; c")


# --- markdown path ----------------------------------------------------------


def test_markdown_rewrites_a_bare_url_to_an_angle_bracketed_link():
    assert markdown_with_citation_links(f"See {DOI} for details.") == (
        f'See [{CITATION_LABEL}](<{DOI}> "{DOI}") for details.'
    )


def test_markdown_rewrites_two_urls_in_one_paragraph_and_keeps_the_parens():
    """The real production shape: two parenthesised DOIs in one pitch. The
    closing bracket belongs to the sentence, not to the URL."""
    out = markdown_with_citation_links(
        f"in ACS Med Chem Lett 2026 ({DOI}), with modelling on bioRxiv ({DOI2})."
    )
    assert out == (
        f'in ACS Med Chem Lett 2026 ([{CITATION_LABEL}](<{DOI}> "{DOI}")), '
        f'with modelling on bioRxiv ([{CITATION_LABEL}](<{DOI2}> "{DOI2}")).'
    )


def test_a_balanced_paren_inside_a_doi_is_not_stripped():
    """``10.1002/(SICI)…`` is a real DOI shape. Only an UNBALANCED trailing
    ``)`` is sentence punctuation."""
    url = "https://doi.org/10.1002/(SICI)1521-3773(19980316)37:5"
    out = markdown_with_citation_links(f"see {url} here")
    assert f"(<{url}>" in out
    assert out.endswith(" here")


def test_markdown_is_idempotent():
    once = markdown_with_citation_links(f"See {DOI}.")
    assert markdown_with_citation_links(once) == once


def test_a_markdown_link_with_real_text_is_left_alone():
    text = f"the [Slusher lab paper]({DOI}) reports"
    assert markdown_with_citation_links(text) == text


def test_a_markdown_link_whose_text_is_its_own_url_is_rewritten():
    assert markdown_with_citation_links(f"see [{DOI}]({DOI}) now") == (
        f'see [{CITATION_LABEL}](<{DOI}> "{DOI}") now'
    )


def test_a_url_inside_a_code_span_is_left_alone():
    text = f"call `curl {DOI}` first"
    assert markdown_with_citation_links(text) == text


def test_a_url_inside_a_fenced_block_is_left_alone():
    text = f"```\ncurl {DOI}\n```"
    assert markdown_with_citation_links(text) == text


def test_an_autolink_is_left_alone():
    text = f"see <{DOI}> now"
    assert markdown_with_citation_links(text) == text


def test_a_double_quote_terminates_the_url_rather_than_entering_the_title():
    """The URL charset excludes ``"``, so a quote ends the match. The half
    before it is still linkified; what matters is that no ``"`` ever reaches
    the title attribute, where it would break the link syntax and leave marked
    rendering the raw markdown to the reader."""
    out = markdown_with_citation_links('see https://x.test/a"b now')
    assert '(<https://x.test/a> "https://x.test/a")' in out
    assert '"b now' in out


# --- plain path -------------------------------------------------------------


def test_plain_linkifies_and_escapes_around_the_link():
    out = plain_with_citation_links(f"a <b> & {DOI} end")
    assert out == Markup(
        "a &lt;b&gt; &amp; "
        f'<a class="citation-link" href="{DOI}" title="{DOI}"'
        f' rel="noreferrer">{CITATION_LABEL}</a>'
        " end"
    )


def test_plain_does_not_double_escape_an_ampersand_bearing_url():
    """The defect this ordering exists to prevent: escaping the whole string
    first would put ``&amp;amp;`` in the href."""
    url = "https://clinicaltrials.gov/search?cond=A&term=B"
    out = plain_with_citation_links(f"see {url}")
    assert f'href="{url.replace("&", "&amp;")}"' in out
    assert "&amp;amp;" not in out


def test_plain_escapes_a_script_tag_in_the_surrounding_prose():
    out = plain_with_citation_links(f"<script>alert(1)</script> {DOI}")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_plain_keeps_the_sentence_paren_outside_the_anchor():
    """The closing bracket and the comma belong to the sentence, not the URL."""
    out = plain_with_citation_links(f"in eLife 2025 ({DOI}), which reports")
    assert f'href="{DOI}"' in out
    assert ">cited paper</a>), which reports" in out


def test_plain_emits_no_target_attribute():
    """DOMPurify 3.1.6 strips ``target`` on the markdown path, so emitting one
    here would make the two render paths disagree — visibly, as a new tab on
    one surface and not the other. ``rel`` is the opposite case and IS
    emitted: DOMPurify keeps it, and it has no visible behaviour to diverge.
    """
    out = plain_with_citation_links(f"see {DOI}")
    assert "target=" not in out
    assert 'rel="noreferrer"' in out


def test_plain_leaves_a_mailto_or_scheme_less_doi_alone():
    text = "write to a@b.test or see doi:10.1021/x"
    assert plain_with_citation_links(text) == Markup(text)


# --- 2026-09-21 audit: the inline-link path had no scheme check -------------


@pytest.mark.parametrize(
    "text",
    [
        "[mailto:pi@jhu.edu](mailto:pi@jhu.edu)",
        "[/admin/users/deadbeef](/admin/users/deadbeef)",
        "[javascript:alert1](javascript:alert1)",
        "[ftp://x.test/a](ftp://x.test/a)",
    ],
)
def test_a_self_titled_link_with_a_non_http_destination_is_left_alone(text):
    """The free-text path was scheme-checked; the inline-link path was not, so
    a self-titled `[mailto:…](mailto:…)` became a "cited paper" link — and
    `[javascript:…](javascript:…)` was inert only because DOMPurify rejects the
    scheme, i.e. a CDN bundle was the last line of defence after the label had
    replaced the one signal the reader had."""
    assert markdown_with_citation_links(text) == text


def test_an_empty_markdown_link_is_not_turned_into_a_citation():
    """`[]()` matched the self-titled branch with both groups empty and became
    `[cited paper](<> "")` — an anchor pointing at the current page."""
    assert markdown_with_citation_links("Nothing here []() at all.") == (
        "Nothing here []() at all."
    )


def test_a_url_ending_in_a_backslash_is_not_linkified():
    """A trailing `\\` escapes the destination's closing `>` and then the
    title's closing `"` under CommonMark, so the inline link never parses and
    marked renders the raw `[cited paper](<https://…` to the reader — exactly
    the failure the angle brackets exist to prevent."""
    out = markdown_with_citation_links("See https://x.test/path\\ now.")
    assert "\\>" not in out
    # Left as TEXT, not linkified up to the backslash: the match was cut short
    # by a character the URL charset excludes, and a truncated destination
    # hidden behind the "cited paper" label is worse than the visible URL
    # (2026-09-21 re-audit).
    assert out == "See https://x.test/path\\ now."


@pytest.mark.parametrize("fn", [markdown_with_citation_links, plain_with_citation_links])
def test_a_bare_scheme_with_no_host_is_not_linkified(fn):
    """`_split_trailing` can peel a match down to `https://`, and a dead link
    presented as a citation is worse than the text it replaced."""
    text = "the endpoint https://, which we saw"
    assert str(fn(text)) == text


def test_the_plain_anchor_carries_rel_noreferrer():
    """Keeps the assessment's own URL out of the Referer sent to whatever host
    the hub cited. Markdown has no syntax for `rel`, so only this path can."""
    out = plain_with_citation_links(f"see {DOI}")
    assert 'rel="noreferrer"' in out


@pytest.mark.parametrize("value", [0, 3.05, True, ["a"], {"k": "v"}])
def test_a_non_string_is_returned_rather_than_raising(value):
    """Call sites pass raw JSONB elements — `key_points` bullets and the hub's
    `strengths`/`risks`/`competitive_landscape`/`evidence_maturity` lists. A
    model-written bullet that arrives as a number used to render as text;
    raising here would 500 the whole list page."""
    assert markdown_with_citation_links(value) == value
    # Escaped, not passed through: a dict or list repr can contain `<`.
    assert str(plain_with_citation_links(value)) == str(escape(value))


# --- 2026-09-21 re-audit: the fixes' own gaps ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f'[t](<{DOI}> "cap")',
        f'[t](<{DOI}>\n"cap")',
        f'[t](<{DOI}>"cap")',
        f"[t](<{DOI}>)",
        f"[t](  <{DOI}>  )",
    ],
)
def test_an_angle_bracketed_inline_link_is_protected(text):
    """`_MD_PROTECTED_RE`'s angle-destination alternative had NO test: the
    whole alternative could be deleted and the suite stayed green. The
    no-whitespace-title form is included because tightening this alternative
    for the ReDoS fix is exactly what nearly dropped it out of the protected
    set — and an unprotected construct has its inner URL rewritten, producing
    nested markdown."""
    assert markdown_with_citation_links(text) == text


def test_the_protected_pattern_is_not_quadratic_in_whitespace():
    """Guards the ReDoS fix, which was otherwise untestable-by-revert. The
    pre-fix pattern took ~0.8s on this input (measured 2026-09-21); 0.1s
    discriminates by an order of magnitude without being flaky on a loaded
    host."""
    import time

    payload = "[](<>" + " " * 20000
    start = time.perf_counter()
    markdown_with_citation_links(payload)
    assert time.perf_counter() - start < 0.1


@pytest.mark.parametrize(
    "url", ["https://", "https:///doi.org/x", "https://?q=1", "https://#frag", "http:///"]
)
def test_a_url_with_no_real_host_is_not_linkified(url):
    """`strip("/")` proved only a non-empty remainder, so `https://?q=1` —
    which has no authority at all — still rendered as a citation. The check is
    now `urlsplit(...).hostname`."""
    text = f"the endpoint {url}, which we saw"
    assert str(plain_with_citation_links(text)) == text
    assert markdown_with_citation_links(text) == text


@pytest.mark.parametrize("fn", [markdown_with_citation_links, plain_with_citation_links])
def test_a_url_truncated_by_a_backslash_is_left_as_text(fn):
    """`\\` is outside the URL charset because it breaks the markdown
    destination — which also means a URL containing one is matched only up to
    it. Linkifying that half would hide the truncation behind the label, on
    BOTH paths."""
    text = "see https://x.test/a\\b for the data"
    assert str(fn(text)) == text
