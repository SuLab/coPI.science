"""The markdown -> Slack mrkdwn conversion, with the approximation tilde pinned.

Slack mrkdwn strikethrough is a SINGLE tilde (`~strike~`,
docs.slack.dev/messaging/formatting-message-text, confirmed 2026-09-14) and there is no
escape for it. This corpus writes `~` to mean "approximately": measured 2026-09-14 over
`agent_messages`, 274 of 1751 messages carry a tilde, 88 carry two or more, and 9 carry a
same-line `~…~` pair Slack strikes through. `markdown_to_mrkdwn` now rewrites `~` to `≈`
outside code spans, Slack link/mention syntax and URLs.

Two of these tests are load-bearing beyond the obvious case:

* `test_an_inequality_pair_does_not_shield_the_tilde_between_them` is the ONE case that
  separates the narrow protected-span pattern from the broad `<[^<>\\n]*>` one an earlier
  draft proposed. This corpus writes `<`/`>` as inequality operators, so the broad matcher
  pairs them across prose and swallows the tilde between — reproducing the bug. Both
  patterns score 0/274 on today's stored messages, so without this probe a future
  "simplification" back to the broad matcher passes every other test in this file.
* `test_the_conversion_never_lengthens_the_string` is `split_for_slack`'s contract: it
  splits the *source* markdown against Slack's 4000-character limit on the guarantee that
  conversion never grows a chunk.
"""

import re

from src.agent.slack_client import markdown_to_mrkdwn

# What Slack itself would render as strikethrough: a same-line tilde pair with non-space
# immediately inside each end.
_STRIKETHROUGH = re.compile(r"~(?=\S)[^~\n]*?(?<=\S)~")


def _strikes_through(text: str) -> bool:
    return _STRIKETHROUGH.search(text) is not None


def _pairable_tildes(text: str) -> bool:
    """Two or more tildes on one line — the input condition for Slack striking anything.

    `_STRIKETHROUGH` is the strict form (non-space immediately inside each end), which is
    how Slack renders `(~3 … (~337`. It does NOT match `~3-4 months, ~$80K`, where a space
    precedes the closing tilde — so the corpus test asserts pairability, the condition the
    conversion actually removes, and checks the strict form as well.
    """
    return any(line.count("~") >= 2 for line in text.splitlines())


def test_a_bare_tilde_becomes_an_approximation_sign():
    assert markdown_to_mrkdwn("Budget is ~$40K.") == "Budget is ≈$40K."


def test_two_tildes_on_one_line_cannot_render_as_strikethrough():
    source = "Per arm this runs ~$80K-~$100K."
    assert _strikes_through(source), "probe must be strike-through-able before conversion"

    converted = markdown_to_mrkdwn(source)

    assert not _strikes_through(converted)
    assert converted == "Per arm this runs ≈$80K-≈$100K."


def test_a_tilde_inside_an_inline_code_span_is_left_alone():
    source = "Run `cd ~/repo && make` first, it takes ~5 minutes."

    converted = markdown_to_mrkdwn(source)

    assert converted == "Run `cd ~/repo && make` first, it takes ≈5 minutes."


def test_a_tilde_inside_a_fenced_block_is_left_alone():
    source = "Before:\n```\nrsync ~/a ~/b\n```\nTakes ~2 hours.\n"

    converted = markdown_to_mrkdwn(source)

    assert converted == "Before:\n```\nrsync ~/a ~/b\n```\nTakes ≈2 hours.\n"


def test_a_tilde_inside_a_slack_link_span_is_left_alone():
    # The exact shape render_assessment_headline emits: "<{permalink}|View interview>".
    source = "Scored 3.1 — <https://example.slack.com/archives/C1/p~99|View interview>, ~$2M ask."

    converted = markdown_to_mrkdwn(source)

    assert "p~99|View interview>" in converted
    assert "≈$2M ask" in converted


def test_a_tilde_inside_a_bare_url_is_left_alone():
    source = "See http://host/~user/notes for the ~40% figure."

    converted = markdown_to_mrkdwn(source)

    assert converted == "See http://host/~user/notes for the ≈40% figure."


def test_a_tilde_inside_a_markdown_link_target_is_left_alone():
    source = "[the notes](http://host/~user/notes) put it at ~40%."

    converted = markdown_to_mrkdwn(source)

    assert converted == "[the notes](http://host/~user/notes) put it at ≈40%."


def test_an_inequality_pair_does_not_shield_the_tilde_between_them():
    # The adversarial probe, verbatim from the 2026-09-14 measurement. A broad `<…>`
    # protected-span matcher pairs `<25 … >0.20` and leaves `~$40K` intact.
    source = "Retrospective cut with <25 per arm at ~$40K or bootstrap CI >0.20 is uninformative."

    converted = markdown_to_mrkdwn(source)

    assert "≈$40K" in converted
    assert "~" not in converted


def test_the_conversion_never_lengthens_the_string():
    cases = [
        "Budget is ~$40K.",
        "**~3–4 months, ~$80–100K, roughly 40% salary**",
        "- ~5 samples\n- **bold** and ~10 more\n",
        "Run `cd ~/repo` then wait ~5 minutes.",
        "```\n~/a ~/b\n```\n",
        "See http://host/~user and ~40%.",
        "<https://example.com/p~1|View> at ~$2M",
        "Retrospective cut with <25 per arm at ~$40K or bootstrap CI >0.20 is uninformative.",
        "No special characters at all.",
    ]

    for source in cases:
        assert len(markdown_to_mrkdwn(source)) <= len(source), source


def test_bold_and_bullet_conversion_is_unchanged():
    assert markdown_to_mrkdwn("**bold** text") == "*bold* text"
    assert markdown_to_mrkdwn("- item\n- other\n") == "• item\n• other\n"
    assert markdown_to_mrkdwn("  - nested") == "  • nested"
    assert markdown_to_mrkdwn("a * b * c") == "a * b * c"


def test_real_corpus_shapes_from_the_2026_09_14_measurement():
    # Four excerpts from production `agent_messages`, each carrying a same-line tilde pair
    # Slack can pair into strikethrough as stored.
    excerpts = [
        "… a FITC-dextran ladder (~3, 10, 70 kDa) … linezolid (~337 Da, neutral …",
        "… NfL (~68 kDa filament fragment) and UCH-L1 (~25 kDa cytosolic enzyme) …",
        "… (~$5-8K/specimen for 8-10 banked specimens (~$60-90K, 3-5 months) …",
        "**~3–4 months, ~$80–100K, roughly 40% salary …",
    ]

    for source in excerpts:
        assert _pairable_tildes(source), source

        converted = markdown_to_mrkdwn(source)

        assert not _pairable_tildes(converted), converted
        assert not _strikes_through(converted), converted
        assert "~" not in converted, converted
