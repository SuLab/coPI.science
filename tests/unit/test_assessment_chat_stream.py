"""Consuming one chat answer (spec §5.4-§5.7) against FakeAsyncAnthropic."""

import logging
from types import SimpleNamespace

import pytest

from src.services.assessment_chat_record import build_chat_record
from src.services.assessment_chat_stream import (
    CitationBook,
    UsageSnapshot,
    billed_sums,
    consume_stream,
    display_cited_text,
    failure_outcome,
    map_stop,
    outcome_from_final,
    rewrite_links,
    strip_private_use,
    usage_entries,
)
from tests.assessment_chat_support import RECORD_URL_IN_PITCH, citation, synthetic_detail
from tests.fakes import ChatScript, FakeAsyncAnthropic

MODEL = "claude-opus-5-5"
OPEN = chr(0xE000)
CLOSE = chr(0xE001)
ALLOWED = frozenset({RECORD_URL_IN_PITCH})
TOKENS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _record():
    return build_chat_record(synthetic_detail(), tier="staff")


async def _run(script: ChatScript):
    """Drive one scripted stream through consume_stream.

    Returns (emitted events, final message, usage snapshot, record)."""
    record = _record()
    events = []

    async def emit(name, data):
        events.append((name, data))

    snapshot = UsageSnapshot()
    fake = FakeAsyncAnthropic([script])
    async with fake.beta.messages.stream(model=MODEL) as stream:
        await consume_stream(stream, emit=emit, snapshot=snapshot, book=CitationBook(record))
        final = await stream.get_final_message()
    return events, final, snapshot, record


async def test_convenience_events_never_double_the_text():
    events, _, _, _ = await _run(ChatScript(segments=[("Exactly once, please.", [])], chunk=3))
    assert "".join(d["text"] for name, d in events if name == "text") == "Exactly once, please."
    assert events[0] == ("status", {"state": "thinking"})
    assert events[1] == ("status", {"state": "answering"})


async def test_segments_and_citations_come_from_the_final_message():
    label = "Message 1 of 5 · VogelsteinBot (the lab's agent) · new_post"
    script = ChatScript(
        segments=[
            ("The lab's agent pitched a panel. ", [citation(1, 0, f"[{label}]\n> :bulb: pitch")]),
            ("The hub asked about controls.", [citation(1, 1), citation(1, 0)]),
        ]
    )
    events, final, _, record = await _run(script)
    outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert outcome.status == "complete"
    assert outcome.answer_text == "The lab's agent pitched a panel. The hub asked about controls."
    assert outcome.segments == [
        {"text": "The lab's agent pitched a panel. ", "cites": [1]},
        {"text": "The hub asked about controls.", "cites": [2, 1]},
    ]
    first, second = outcome.citations
    assert first == {
        "n": 1, "doc": "interview", "anchor": "m-msg-1", "label": label, "cited_text": ":bulb: pitch",
    }
    assert (second["n"], second["anchor"]) == (2, "m-msg-2")
    streamed = [d for name, d in events if name == "citation"]
    assert [c["citation"]["n"] for c in streamed] == [1, 2, 1]
    assert [c["seg"] for c in streamed] == [0, 1, 1]


async def test_an_unmapped_citation_keeps_no_anchor_and_warns(caplog):
    _, final, _, record = await _run(ChatScript(segments=[("Out of range.", [citation(9, 0)])]))
    with caplog.at_level(logging.WARNING, logger="src.services.assessment_chat_stream"):
        outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert outcome.citations == [
        {"n": 1, "doc": None, "anchor": None, "label": "the record", "cited_text": "cited"}
    ]
    assert "maps to no record block" in caplog.text


@pytest.mark.parametrize(
    "stop_reason,text,status,error_code",
    [
        ("end_turn", "An answer.", "complete", None),
        ("end_turn", "   ", "failed", "empty_answer"),
        ("max_tokens", "Half an ans", "truncated", None),
        ("max_tokens", "", "failed", "empty_answer"),
        ("refusal", "Partial", "refused", None),
        ("pause_turn", "Text", "failed", "unexpected_stop"),
    ],
)
def test_map_stop(stop_reason, text, status, error_code):
    assert map_stop(stop_reason, text) == (status, error_code)


async def test_max_tokens_with_no_text_block_is_a_failed_empty_answer():
    _, final, _, record = await _run(ChatScript(segments=[], stop_reason="max_tokens"))
    outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert (outcome.status, outcome.error_code, outcome.answer_text) == ("failed", "empty_answer", "")


@pytest.mark.parametrize(
    "details,category",
    [({"type": "refusal", "category": "bio", "explanation": "x"}, "bio"), (None, None)],
)
async def test_a_refusal_discards_the_answer_and_keeps_the_category(details, category):
    script = ChatScript(
        segments=[("A partial answer that was refused", [citation(1, 0)])],
        stop_reason="refusal",
        stop_details=details,
    )
    _, final, _, record = await _run(script)
    outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert outcome.status == "refused"
    assert outcome.refusal_category == category
    assert (outcome.answer_text, outcome.segments, outcome.citations, outcome.allowed_links) == (
        "", [], [], [],
    )


def _iteration(kind, model, output_tokens, **tokens):
    base = dict.fromkeys(TOKENS, 0)
    base.update(tokens, output_tokens=output_tokens)
    return {"type": kind, "model": model, **base}


async def test_sticky_fallback_routing_is_detected_from_the_served_model():
    script = ChatScript(
        model="claude-opus-5",
        iterations=[_iteration("message", "claude-opus-5", 5, input_tokens=10)],
    )
    _, final, _, record = await _run(script)
    outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert (outcome.served_by_model, outcome.fallback_used) == ("claude-opus-5", True)


async def test_a_mid_stream_fallback_is_detected_from_the_iterations():
    script = ChatScript(
        model=MODEL,
        iterations=[
            _iteration("message", MODEL, 40, input_tokens=100, cache_creation_input_tokens=900),
            _iteration("fallback_message", "claude-opus-4-8", 60, input_tokens=120),
        ],
    )
    _, final, _, record = await _run(script)
    outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert (outcome.served_by_model, outcome.fallback_used) == ("claude-opus-4-8", True)
    assert [e["billed"] for e in outcome.usage_by_model] == [True, True]


async def test_a_pre_output_decline_is_recorded_but_not_billed():
    script = ChatScript(
        model="claude-opus-5",
        fallback=(MODEL, "claude-opus-5"),
        iterations=[
            _iteration("message", MODEL, 0, input_tokens=5000),
            _iteration("fallback_message", "claude-opus-5", 70, input_tokens=5000),
        ],
    )
    events, final, _, _ = await _run(script)
    assert ("notice", {"kind": "fallback", "from_model": MODEL, "to_model": "claude-opus-5"}) in events
    entries = usage_entries(final)
    assert entries[0] == {
        "model": MODEL, "billed": False, "input_tokens": 5000, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    assert entries[1]["billed"] is True
    assert billed_sums(entries) == {
        "input_tokens": 5000, "output_tokens": 70, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_top_level_usage_without_iterations():
    final = SimpleNamespace(
        model=MODEL,
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=2, cache_read_input_tokens=3,
            cache_creation_input_tokens=4, iterations=None,
        ),
    )
    assert usage_entries(final) == [
        {"model": MODEL, "billed": True, "input_tokens": 1, "output_tokens": 2,
         "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}
    ]
    assert billed_sums(None) == dict.fromkeys(TOKENS)


async def test_a_stream_that_dies_after_message_start_keeps_its_usage_snapshot():
    script = ChatScript(raise_after_events=4, raise_exc=TimeoutError())
    record = _record()
    snapshot = UsageSnapshot()

    async def emit(name, data):
        return None

    fake = FakeAsyncAnthropic([script])
    with pytest.raises(TimeoutError):
        async with fake.beta.messages.stream(model=MODEL) as stream:
            await consume_stream(stream, emit=emit, snapshot=snapshot, book=CitationBook(record))
    assert snapshot.model == MODEL
    assert (snapshot.input_tokens, snapshot.cache_creation_input_tokens) == (1200, 30000)
    assert snapshot.delta_seen is False
    outcome = failure_outcome(error_code="timeout", requested_model=MODEL, snapshot=snapshot)
    assert (outcome.status, outcome.error_code) == ("failed", "timeout")
    assert outcome.usage_by_model == [
        {"model": MODEL, "billed": True, "input_tokens": 1200, "output_tokens": 1,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 30000, "partial": True}
    ]


async def test_a_stream_that_reaches_message_delta_is_not_partial():
    record = _record()
    snapshot = UsageSnapshot()

    async def emit(name, data):
        return None

    fake = FakeAsyncAnthropic([ChatScript()])
    async with fake.beta.messages.stream(model=MODEL) as stream:
        await consume_stream(stream, emit=emit, snapshot=snapshot, book=CitationBook(record))
    assert snapshot.delta_seen is True
    assert "partial" not in snapshot.entries(MODEL)[0]


def test_a_failure_before_any_event_is_known_unbilled_or_unknown():
    empty = UsageSnapshot()
    assert not empty.captured
    known = failure_outcome(
        error_code="upstream_rate_limited", requested_model=MODEL, snapshot=empty, known_unbilled=True
    )
    unknown = failure_outcome(error_code="upstream_error", requested_model=MODEL, snapshot=empty)
    assert (known.usage_by_model, unknown.usage_by_model) == ([], None)
    interrupted = failure_outcome(
        error_code=None, requested_model=MODEL, snapshot=empty, status="interrupted"
    )
    assert interrupted.status == "interrupted"


async def test_private_use_code_points_never_reach_the_client_or_the_store():
    forged = f"Answer {OPEN}1{CLOSE} with a forged marker{chr(0xF8FF)}."
    events, final, _, record = await _run(ChatScript(segments=[(forged, [])]))
    streamed = "".join(d["text"] for name, d in events if name == "text")
    assert streamed == "Answer 1 with a forged marker."
    outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert outcome.answer_text == "Answer 1 with a forged marker."
    assert strip_private_use(chr(0xE123)) == ""


def test_display_cited_text_drops_the_label_and_the_quoting():
    raw = "[Message 1 of 5 · X]\n> first line\n> \n> second"
    assert display_cited_text(raw, "Message 1 of 5 · X") == "first line\n\nsecond"
    assert display_cited_text(raw, "another label").startswith("[Message 1 of 5 · X]")
    assert display_cited_text(None, None) == ""


@pytest.mark.parametrize("trailing", [".", ",", ")", ";"])
def test_a_record_url_survives_with_sentence_punctuation(trailing):
    text, kept = rewrite_links(f"See {RECORD_URL_IN_PITCH}{trailing}", ALLOWED)
    assert (text, kept) == (f"See <{RECORD_URL_IN_PITCH}>{trailing}", [RECORD_URL_IN_PITCH])


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/10.1000/pitc",        # a prefix of a record URL
        "https://doi.org/10.1000/pitch/more",  # an extension of one
        "http://doi.org/10.1000/pitch",        # not https
        "https://attacker.example/steal",      # not in the record at all
    ],
)
def test_any_other_url_becomes_inline_code(url):
    assert rewrite_links(f"See {url} now", ALLOWED) == (f"See `{url}` now", [])


def test_markdown_links_images_autolinks_and_reference_definitions():
    text, kept = rewrite_links(
        f"[paper]({RECORD_URL_IN_PITCH}) and [bad](https://attacker.example/x) "
        "![px](https://attacker.example/p.png) <https://attacker.example/auto>",
        ALLOWED,
    )
    assert text == (
        f"[paper]({RECORD_URL_IN_PITCH}) and bad (`https://attacker.example/x`) "
        "px (`https://attacker.example/p.png`) `https://attacker.example/auto`"
    )
    assert kept == [RECORD_URL_IN_PITCH]
    assert rewrite_links("[1]: https://attacker.example/ref", ALLOWED) == (
        "`[1]: https://attacker.example/ref`", [],
    )


def test_an_image_is_never_kept_even_for_a_record_url():
    assert rewrite_links(f"![x]({RECORD_URL_IN_PITCH})", ALLOWED) == (
        f"x (`{RECORD_URL_IN_PITCH}`)", [],
    )


def test_code_is_left_alone():
    fence = "`" * 3
    code = f"Run `curl https://attacker.example/x` or\n{fence}\nhttps://attacker.example/y\n{fence}"
    assert rewrite_links(code, ALLOWED) == (code, [])


def test_a_www_host_is_made_inert():
    assert rewrite_links("Visit www.attacker.example today", ALLOWED) == (
        "Visit `www.attacker.example` today", [],
    )


def test_non_ascii_urls_compare_after_tokenization():
    url = "https://example.org/données"
    assert rewrite_links(f"See {url}.", frozenset({url})) == (f"See <{url}>.", [url])


def test_a_backtick_inside_an_inert_url_cannot_close_its_code_span():
    assert rewrite_links("[x](https://attacker.example/a`b)", ALLOWED)[0] == (
        "x (``https://attacker.example/a`b``)"
    )


def test_an_allowed_link_label_keeps_bare_urls_bare():
    # Inside a link label marked never autolinks, so bracketing would nest anchors.
    text, kept = rewrite_links(f"[see {RECORD_URL_IN_PITCH}]({RECORD_URL_IN_PITCH})", ALLOWED)
    assert (text, kept) == (
        f"[see {RECORD_URL_IN_PITCH}]({RECORD_URL_IN_PITCH})", [RECORD_URL_IN_PITCH],
    )


def test_a_record_url_wrapped_in_bold_emphasis_is_still_kept():
    text, kept = rewrite_links(f"See **{RECORD_URL_IN_PITCH}**", ALLOWED)
    assert (text, kept) == (f"See **<{RECORD_URL_IN_PITCH}>**", [RECORD_URL_IN_PITCH])


def test_a_record_url_wrapped_in_underscore_emphasis_with_trailing_period():
    text, kept = rewrite_links(f"See _{RECORD_URL_IN_PITCH}_.", ALLOWED)
    assert (text, kept) == (f"See _<{RECORD_URL_IN_PITCH}>_.", [RECORD_URL_IN_PITCH])


@pytest.mark.parametrize(
    "text",
    [
        "See HTTPS://EXAMPLE.COM/x now",
        "Grab ftp://ftp.example.com/file.zip please",
        "Email pi@jhu.edu for details",
    ],
)
def test_new_url_and_email_shapes_become_code(text):
    result, kept = rewrite_links(text, ALLOWED)
    assert kept == []
    assert "`" in result
    assert result != text


def test_ftp_and_uppercase_scheme_are_exact():
    assert rewrite_links("Grab ftp://ftp.example.com/file.zip please", ALLOWED) == (
        "Grab `ftp://ftp.example.com/file.zip` please", [],
    )
    assert rewrite_links("See HTTPS://EXAMPLE.COM/x now", ALLOWED) == (
        "See `HTTPS://EXAMPLE.COM/x` now", [],
    )


def test_raw_html_and_comments_become_code_but_autolinks_still_work():
    text, kept = rewrite_links(
        f"<div>text</div> and <!-- a comment --> then <{RECORD_URL_IN_PITCH}>", ALLOWED
    )
    assert kept == [RECORD_URL_IN_PITCH]
    assert "`<div>`" in text
    assert "`</div>`" in text
    assert "`<!-- a comment -->`" in text
    assert f"<{RECORD_URL_IN_PITCH}>" in text


def test_a_refdef_inside_a_blockquote_becomes_code():
    assert rewrite_links("> [1]: https://attacker.example/ref", ALLOWED) == (
        "`> [1]: https://attacker.example/ref`", [],
    )


def test_a_refdef_inside_a_list_item_becomes_code():
    assert rewrite_links("- [1]: https://attacker.example/ref", ALLOWED) == (
        "`- [1]: https://attacker.example/ref`", [],
    )


def test_an_image_label_bang_is_escaped_so_no_image_can_form():
    text, kept = rewrite_links(
        f"[![a](https://attacker.example/p.png)]({RECORD_URL_IN_PITCH})", ALLOWED
    )
    assert "![" not in text
    assert RECORD_URL_IN_PITCH in kept


@pytest.mark.parametrize(
    "text",
    [
        f"See {RECORD_URL_IN_PITCH}.",
        f"See {RECORD_URL_IN_PITCH} now",
        f"[paper]({RECORD_URL_IN_PITCH})",
        "[bad](https://attacker.example/x)",
        "![px](https://attacker.example/p.png)",
        "<https://attacker.example/auto>",
        f"<{RECORD_URL_IN_PITCH}>",
        "[1]: https://attacker.example/ref",
        "> [1]: https://attacker.example/ref",
        "Visit www.attacker.example today",
        "Email pi@jhu.edu for details",
        "<div>text</div>",
        "<!-- a comment -->",
        f"**{RECORD_URL_IN_PITCH}**",
        f"[![a](https://attacker.example/p.png)]({RECORD_URL_IN_PITCH})",
        f"[`https://attacker.example/x` paper]({RECORD_URL_IN_PITCH})",
        f"[see <{RECORD_URL_IN_PITCH}>]({RECORD_URL_IN_PITCH})",
        "\\`https://attacker.example/x\\`",
        "\\[x](https://attacker.example/y)",
        "[x](https://attacker.example/a`b)",
        "``https://attacker.example/a`b``",
        "Run `curl https://attacker.example/x` or\n```\nhttps://attacker.example/y\n```",
    ],
)
def test_rewrite_links_is_idempotent(text):
    once, _ = rewrite_links(text, ALLOWED)
    twice, _ = rewrite_links(once, ALLOWED)
    assert twice == once


def test_backslash_escapes_are_read_as_marked_reads_them():
    # An escaped backtick opens no code span, so the URL after it is live text to
    # marked and must be coded; an escaped bracket opens no link, so what follows it
    # is judged as plain text.
    text, kept = rewrite_links("\\`https://attacker.example/x\\`", ALLOWED)
    assert text == "\\``https://attacker.example/x`\\`"
    assert kept == []
    text, kept = rewrite_links("\\[x](https://attacker.example/y)", ALLOWED)
    assert text == "\\[x](`https://attacker.example/y`)"
    assert kept == []


def test_a_code_span_inside_a_link_label_is_left_alone():
    text, kept = rewrite_links(f"[`https://attacker.example/x` paper]({RECORD_URL_IN_PITCH})", ALLOWED)
    assert text == f"[`https://attacker.example/x` paper]({RECORD_URL_IN_PITCH})"
    assert kept == [RECORD_URL_IN_PITCH]


def test_an_allowed_autolink_inside_an_allowed_label_stays_bare():
    # marked never nests anchors; a `<url>` inside the label would try to.
    text, _ = rewrite_links(f"[see <{RECORD_URL_IN_PITCH}>]({RECORD_URL_IN_PITCH})", ALLOWED)
    assert text == f"[see {RECORD_URL_IN_PITCH}]({RECORD_URL_IN_PITCH})"


async def test_a_link_split_across_a_citation_boundary_collapses_to_one_segment(caplog):
    script = ChatScript(
        segments=[
            ("see https:", [citation(1, 0)]),
            ("//attacker.example/x", [citation(1, 1)]),
        ]
    )
    _, final, _, record = await _run(script)
    with caplog.at_level(logging.WARNING, logger="src.services.assessment_chat_stream"):
        outcome = outcome_from_final(final, record=record, requested_model=MODEL)
    assert len(outcome.segments) == 1
    assert outcome.segments[0]["cites"] == [1, 2]
    assert outcome.answer_text == "see `https://attacker.example/x`"
    assert outcome.allowed_links == []
    assert "spanned a citation boundary" in caplog.text


def test_strip_private_use_repeats_until_no_reference_remains():
    # Removing the inner reference joins its neighbours into a new one.
    assert strip_private_use("a&#&#xE000;57344;b") == "ab"
    assert strip_private_use("&#5734" + chr(0xE000) + "4;") == ""
    # A digit run far past Python's int() limit is not a private-use value.
    long_ref = "&#" + "9" * 5000 + ";"
    assert strip_private_use(long_ref) == long_ref


def test_strip_private_use_removes_numeric_references():
    assert strip_private_use("marker &#57344; end") == "marker  end"
    assert strip_private_use("marker &#xE000; end") == "marker  end"
    assert strip_private_use("marker &#XE000 end") == "marker  end"
    assert strip_private_use("marker &#0057344; end") == "marker  end"
    # Not private-use, and not touched.
    assert strip_private_use("&amp; &#65; &#x41;") == "&amp; &#65; &#x41;"
