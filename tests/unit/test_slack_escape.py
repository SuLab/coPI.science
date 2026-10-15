"""R1.9: escape_for_slack (C9 of the 2026-10-15 publication-handling design). Phase 2
keeps every case here unchanged as the agreement check for its find_dois swap (I6)."""
import pytest

from src.agent.slack_escape import escape_for_slack

SICI = "10.1002/1097-0134(20010201)42:2<279::AID-PROT150>3.0.CO;2-U"
SICI_ENCODED = "10.1002/1097-0134(20010201)42:2%3C279::AID-PROT150%3E3.0.CO;2-U"

CASES = {
    "broadcast": ("<!channel> hi <!here> <!everyone> <!subteam^S123|team>",
                  "&lt;!channel&gt; hi &lt;!here&gt; &lt;!everyone&gt; &lt;!subteam^S123|team&gt;"),
    "tokens": ("<@U123ABC> see <#C0456|general> and <https://example.org/a?b=1&c=2|R&D notes> "
               "or <mailto:a@b.org>",
               "<@U123ABC> see <#C0456|general> and <https://example.org/a?b=1&c=2|R&amp;D notes> "
               "or <mailto:a@b.org>"),
    "stray": ("(<15 kDa) & p<0.05 > 3", "(&lt;15 kDa) &amp; p&lt;0.05 &gt; 3"),
    "entities": ("R&amp;D &lt;b&gt; &copy;", "R&amp;D &lt;b&gt; &amp;copy;"),
    "sici_link": (f"<https://doi.org/{SICI}|our paper>", f"<https://doi.org/{SICI_ENCODED}|our paper>"),
    "sici_bare_url": (f"see https://doi.org/{SICI} now", f"see https://doi.org/{SICI_ENCODED} now"),
    "hash": ("https://doi.org/10.1002/(SICI)1097-0134(199908)36:2<x>3.0.CO;2-#",
             "https://doi.org/10.1002/(SICI)1097-0134(199908)36:2%3Cx%3E3.0.CO;2-%23"),
    "sici_no_url": (SICI, "10.1002/1097-0134(20010201)42:2&lt;279::AID-PROT150&gt;3.0.CO;2-U"),
    "paren_markdown": ("[x](https://doi.org/10.1016/S0092-8674(02)00722-5)",
                       "[x](https://doi.org/10.1016/S0092-8674(02)00722-5)"),
    "permalink": (" — <https://w.slack.com/archives/C1/p1?thread_ts=1.2&cid=C1|View interview>",
                  " — <https://w.slack.com/archives/C1/p1?thread_ts=1.2&cid=C1|View interview>"),
    "code": ("`a<b`", "`a&lt;b`"),
    "flood": ("&" * 100, "&amp;" * 100),
}


def _check(key):
    source, expected = CASES[key]
    assert escape_for_slack(source) == expected


def test_broadcast_tokens_are_entity_escaped():
    _check("broadcast")


def test_user_channel_and_link_tokens_are_kept():
    _check("tokens")


def test_stray_control_characters_are_escaped():
    _check("stray")


def test_existing_entities_are_not_double_escaped():
    _check("entities")


def test_a_sici_doi_link_is_percent_encoded():
    _check("sici_link")


def test_a_bare_sici_doi_url_is_percent_encoded():
    _check("sici_bare_url")


def test_a_hash_in_a_doi_url_is_percent_encoded():
    _check("hash")


def test_a_bare_sici_doi_without_url_is_entity_escaped():
    _check("sici_no_url")


def test_a_paren_doi_markdown_link_is_unchanged():
    _check("paren_markdown")


def test_the_headline_permalink_token_is_kept():
    _check("permalink")


def test_code_spans_are_escaped_too():
    _check("code")


def test_an_ampersand_run_is_escaped_in_full():
    _check("flood")


@pytest.mark.parametrize("key", sorted(CASES))
def test_escape_is_idempotent_and_bounded(key):
    source, _ = CASES[key]
    once = escape_for_slack(source)
    assert escape_for_slack(once) == once
    assert len(once) <= 5 * len(source)
