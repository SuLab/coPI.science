"""The read-time interview reconstruction in src/services/assessment_detail.py.

Every function here is pure, which is deliberate: the value of this page is
almost entirely in the correlation between what the hub *logged* and what it
*posted*, and that correlation has to be testable without a database, a
simulation run, or an LLM. The fixtures below are shaped from real production
payloads (run 88d81cd8): a ``thinking`` block with an empty body and a
signature, a consult result prefixed with "<Title> — signal: <signal>", and a
logged response whose ``<slack_message>`` body differs from the stored message
by exactly one leading newline.
"""

from __future__ import annotations

from src.services.assessment_detail import (
    PREFIX_MATCH_CHARS,
    RESULT_EXCERPT_CHARS,
    consult_opinion_from_result,
    correlate_turns_to_messages,
    extract_slack_message,
    normalize_for_match,
    strip_assessment_sidecar,
    tool_chips_from_conversation,
    visible_body,
)

CONSULT_RESULT = (
    "Scientific Specialist — signal: caution\n\n"
    '{\n  "verdict_signal": "caution",\n'
    '  "concerns": ["No isogenic control", "n=3 with no power calculation"],\n'
    '  "questions_to_ask": ["Which control did you use?"],\n'
    '  "confidence": "moderate"\n}'
)


def _conversation(*, include_thinking: bool = True) -> list[dict]:
    """A hub turn that called two tools in one round, shaped like production."""
    assistant_content: list[dict] = []
    if include_thinking:
        assistant_content.append(
            {"type": "thinking", "thinking": "", "signature": "CAISiyIKhwEIEBgCKkBw"}
        )
    assistant_content += [
        {"type": "text", "text": "Let me ground this first."},
        {
            "type": "tool_use",
            "id": "toolu_a",
            "name": "search_prior_art",
            "input": {"query": "TFEB melanoma"},
        },
        {
            "type": "tool_use",
            "id": "toolu_b",
            "name": "consult_specialist",
            "input": {
                "domain": "scientific",
                "question": "Is the counter-screen able to report what they claim?",
                "context": "The PI said they used a parental line.",
            },
        },
    ]
    return [
        {"role": "user", "content": "# Phase 4: Scouting Interview Reply\n\n..."},
        {"role": "assistant", "content": assistant_content},
        {
            "role": "user",
            "content": [
                # Deliberately in the OPPOSITE order to the calls: results are
                # matched by tool_use_id, and a positional pairing would swap
                # the patent hit onto the specialist chip.
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_b",
                    "content": CONSULT_RESULT,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_a",
                    "content": "Source: USPTO Open Data Portal — no title hits.",
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------


def test_extract_slack_message_anchors_on_the_last_tag_pair():
    """A model that mentions the tag while reasoning must not anchor the match —
    otherwise the reasoning is pulled into the body and nothing correlates."""
    raw = (
        "My output is a single <slack_message> block, as instructed.\n"
        "<slack_message>\nThe real reply.\n</slack_message>"
    )
    assert extract_slack_message(raw).strip() == "The real reply."


def test_extract_slack_message_falls_back_to_the_whole_text():
    assert extract_slack_message("no tags here").strip() == "no tags here"


def test_strip_assessment_sidecar_removes_closed_and_truncated_blocks():
    closed = "Body text.\n<assessment_json>{\"scores\": {}}</assessment_json>"
    assert strip_assessment_sidecar(closed).strip() == "Body text."
    # Truncated mid-sidecar (the max_tokens case): everything from the orphaned
    # opening tag to the end goes, or the verdict JSON would be compared against
    # a posted message that never carried it.
    truncated = "Body text.\n<assessment_json>{\"scores\": {\"team\": 4"
    assert strip_assessment_sidecar(truncated).strip() == "Body text."
    # A stray closing tag with no opener is mopped up too.
    assert "assessment_json" not in strip_assessment_sidecar("Body.</assessment_json>")


def test_visible_body_matches_the_stored_message_across_the_leading_newline():
    """The production difference, exactly: the logged body starts with a newline
    inside the tag and the stored message does not. Un-normalized comparison
    matched 12 of 726 rows; this is what makes it 111 of 116."""
    logged = (
        "<slack_message>\n⏸️ Thank you for conceding both points.\n</slack_message>\n"
        "<assessment_json>{\"recommendation\": \"pass\"}</assessment_json>"
    )
    stored = "⏸️ Thank you for  conceding both points."
    assert visible_body(logged) == normalize_for_match(stored)


# ---------------------------------------------------------------------------
# Tool-conversation parsing
# ---------------------------------------------------------------------------


def test_tool_chips_pair_results_by_id_not_by_position():
    chips = tool_chips_from_conversation(_conversation())
    assert [c["tool"] for c in chips] == ["search_prior_art", "consult_specialist"]
    assert "USPTO" in chips[0]["result_full"]
    assert chips[0]["is_consult"] is False
    assert chips[1]["is_consult"] is True
    assert chips[1]["domain"] == "scientific"
    assert "no title hits" not in chips[1]["result_full"]


def test_tool_chips_summarize_the_input_without_dumping_it():
    chips = tool_chips_from_conversation(_conversation())
    assert chips[0]["summary"] == "query: TFEB melanoma"
    # domain leads the consult summary, and the question follows it.
    assert chips[1]["summary"].startswith("domain: scientific")
    assert "counter-screen" in chips[1]["summary"]


def test_tool_chips_carry_the_parsed_specialist_opinion():
    consult = tool_chips_from_conversation(_conversation())[1]
    assert consult["opinion"]["verdict_signal"] == "caution"
    assert consult["opinion"]["confidence"] == "moderate"
    assert consult["opinion"]["concerns"] == [
        "No isogenic control",
        "n=3 with no power calculation",
    ]
    assert consult["opinion"]["questions_to_ask"] == ["Which control did you use?"]


def test_tool_chips_skip_thinking_blocks_and_tolerate_junk():
    """A thinking block is a signature and no reader value; a payload that is not
    a list at all (the factories' default is a dict) must not raise."""
    with_thinking = tool_chips_from_conversation(_conversation())
    without = tool_chips_from_conversation(_conversation(include_thinking=False))
    assert with_thinking == without
    assert tool_chips_from_conversation({"messages": []}) == []
    assert tool_chips_from_conversation(None) == []
    assert tool_chips_from_conversation([{"role": "user", "content": "plain"}]) == []


def test_tool_chip_result_excerpt_is_bounded():
    long_result = "x" * (RESULT_EXCERPT_CHARS * 3)
    conversation = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "retrieve_full_text",
                 "input": {"pmid": "30593499"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": long_result},
            ],
        },
    ]
    chip = tool_chips_from_conversation(conversation)[0]
    assert len(chip["result_excerpt"]) <= RESULT_EXCERPT_CHARS + 1  # + the ellipsis
    assert chip["result_excerpt"].endswith("…")
    assert chip["summary"] == "pmid: 30593499"


def test_a_tool_use_with_no_result_is_still_a_chip():
    """The turn can end before a tool returns (a stop mid-round). Dropping the
    call would make the page claim the hub never made it."""
    conversation = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "search_prior_art",
                 "input": {"query": "C9orf72 repeat"}},
            ],
        },
    ]
    chip = tool_chips_from_conversation(conversation)[0]
    assert chip["no_result"] is True
    assert chip["result_full"] == ""


def test_consult_opinion_is_none_when_the_consult_failed():
    """Every failure path in _execute_consult_specialist returns prose with no
    signal line. None of them may render as a specialist verdict — "the
    specialist was unreachable" must never read as "the specialist approved"."""
    for failure in (
        "Error consulting the chemistry specialist: overloaded_error",
        "The chemistry specialist is unavailable (persona file missing). "
        "Proceed without this opinion; it will not count as consulted.",
        "Unknown specialist domain 'astrology'. Valid domains: budget, chemistry",
        "",
    ):
        assert consult_opinion_from_result(failure, domain="chemistry") is None


def test_consult_opinion_falls_back_to_the_prefix_signal_for_a_prose_reply():
    """Prose IS an opinion (parse_opinion's contract), and the engine's own
    parse is already in the prefix line — so the signal survives even with no
    JSON body to read concerns out of."""
    opinion = consult_opinion_from_result(
        "Chemistry Specialist — signal: blocking\n\nThis series has no path to a DC.",
        domain="chemistry",
    )
    assert opinion == {
        "verdict_signal": "blocking",
        "confidence": None,
        "concerns": [],
        "questions_to_ask": [],
    }


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def _message(key: str, content: str) -> dict:
    return {"key": key, "content_normalized": normalize_for_match(content)}


def test_correlate_matches_a_turn_to_the_message_it_posted():
    messages = [_message("m1", "First reply."), _message("m2", "Second reply.")]
    turns = [
        {"log_id": "l1", "body": "Second reply."},
        {"log_id": "l2", "body": "First reply."},
    ]
    matched, unplaced = correlate_turns_to_messages(turns, messages)
    assert unplaced == []
    assert [t["log_id"] for t in matched["m1"]] == ["l2"]
    assert [t["log_id"] for t in matched["m2"]] == ["l1"]


def test_correlate_keeps_an_unmatched_turn_instead_of_dropping_it():
    """A reply that was edited, mention-stripped or truncated after logging is
    still evidence of what the hub did. Dropping it would make the page look
    complete when it is not."""
    matched, unplaced = correlate_turns_to_messages(
        [{"log_id": "l1", "body": "A reply nobody stored."}],
        [_message("m1", "Something else entirely.")],
    )
    assert matched == {}
    assert [t["log_id"] for t in unplaced] == ["l1"]


def test_correlate_falls_back_to_a_long_prefix():
    """Production has turns whose stored message differs only in the tail (a
    stripped mention). A long prefix recovers those; a SHORT body must not get
    the same treatment, or two replies opening the same way would collide."""
    head = "The mechanism claim is the crux here, and the counter-screen is what "
    head += "decides it. " * 3
    assert len(head) > PREFIX_MATCH_CHARS
    matched, unplaced = correlate_turns_to_messages(
        [{"log_id": "l1", "body": head + "TAIL THAT WAS STRIPPED"}],
        [_message("m1", head + "different ending")],
    )
    assert not unplaced
    assert [t["log_id"] for t in matched["m1"]] == ["l1"]

    short_matched, short_unplaced = correlate_turns_to_messages(
        [{"log_id": "l2", "body": "Thanks!"}],
        [_message("m2", "Thanks for that.")],
    )
    assert short_matched == {}
    assert len(short_unplaced) == 1


def test_correlate_ignores_empty_messages():
    """An empty stored content must not become the bucket every turn falls into."""
    matched, unplaced = correlate_turns_to_messages(
        [{"log_id": "l1", "body": ""}], [_message("m1", "   ")]
    )
    assert matched == {}
    assert len(unplaced) == 1
