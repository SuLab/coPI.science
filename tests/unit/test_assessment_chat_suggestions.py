"""The chat's opening questions, without a database: the template set, the request
the worker sends, and how a reply is read (src/services/assessment_chat_suggestions.py)."""

import json
from types import SimpleNamespace

import pytest

from src.services import assessment_chat_suggestions as sug
from src.services.assessment_chat import FALLBACK_BETA
from src.services.assessment_chat_record import build_chat_record
from tests.assessment_chat_support import synthetic_detail


def _record(tier="staff"):
    return build_chat_record(synthetic_detail(), tier=tier)


def _reply(payload, *, stop_reason="end_turn", text=None):
    body = text if text is not None else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=body)],
        stop_reason=stop_reason,
        stop_details=None,
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=90_000, output_tokens=800, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, iterations=None,
        ),
    )


def _block(record, label_start):
    """The id of the first Verdict block whose label starts with ``label_start``."""
    return next(
        bid for bid, target in sug.verdict_blocks(record).items()
        if target.label.startswith(label_start)
    )


# ---------------------------------------------------------------------------
# The template set
# ---------------------------------------------------------------------------


def test_the_template_set_asks_about_this_verdict():
    detail = synthetic_detail(gating_definitions={
        "credible_science": {"title": "Credible science", "description": "d"},
    })
    got = sug.template_suggestions(detail)
    assert [s.kind for s in got] == ["template"] * 4
    assert [s.anchor for s in got] == ["gating", "scores", "red-flags", "verdict"]
    texts = [s.text for s in got]
    # The NOT-MET gate wins over the unconfirmed one, by its rubric title.
    assert texts[0].startswith("Why did the hub find the gate “Credible science” not met")
    # The lowest scored dimension; the unscored one is not "lowest".
    assert texts[1] == (
        "Why did the hub score Scientific credibility 4 out of 5, and what in the"
        " interview supports that?"
    )
    assert "“RED-FLAG-ONE”" in texts[2]
    assert texts[3] == "What would have to be true for this to move from conditional to advance?"


def test_an_unconfirmed_gate_is_asked_about_when_none_is_not_met():
    detail = synthetic_detail()
    detail["assessment"].gating = {"life_sciences_domain": "met", "translational_potential": "unconfirmed"}
    first = sug.template_suggestions(detail)[0]
    assert first.anchor == "gating"
    assert first.text == (
        "The gate “translational potential” was never confirmed. What does the interview"
        " say about it?"
    )


def test_a_bare_verdict_falls_back_to_the_generic_questions():
    detail = synthetic_detail(dimensions=[])
    a = detail["assessment"]
    a.gating, a.red_flags, a.recommendation, a.recommended_next_experiment = None, None, None, None
    got = sug.template_suggestions(detail)
    assert [s.text for s in got] == list(sug.GENERIC_QUESTIONS)
    assert all(s.anchor is None and s.section is None for s in got)


def test_malformed_stored_values_are_skipped_not_raised():
    detail = synthetic_detail(dimensions=[{"title": "X", "score": "high"}, "junk", {"score": True}])
    a = detail["assessment"]
    a.gating, a.red_flags = ["not", "a", "dict"], "not a list"
    a.recommendation = "advance"
    got = sug.template_suggestions(detail)
    assert [s.anchor for s in got] == ["verdict", "ask"]


def test_a_long_red_flag_is_clipped_to_one_line():
    detail = synthetic_detail()
    detail["assessment"].red_flags = ["word\n" * 60]
    flag = next(s for s in sug.template_suggestions(detail) if s.anchor == "red-flags")
    assert "\n" not in flag.text and "…" in flag.text and len(flag.text) < 160


@pytest.mark.parametrize("anchor,section", [
    ("score-rationale", "brief"), ("red-flags", "red-flags"), ("scores", "scores"),
    ("m-1234", None), ("consult-2", None), (None, None),
])
def test_a_suggestion_shows_inline_only_in_a_known_section(anchor, section):
    assert sug.Suggestion("q", anchor, sug.KIND_GENERATED).section == section


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def test_the_request_is_structured_output_over_the_tiers_record():
    record = _record()
    req = sug.build_request(model="claude-opus-5", system_prompt="SYSTEM", record=record)
    assert req["model"] == "claude-opus-5"
    assert req["max_tokens"] == 6000
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": sug.SUGGESTIONS_SCHEMA}
    assert (req["betas"], req["fallbacks"]) == ([FALLBACK_BETA], "default")
    assert req["system"] == "SYSTEM"
    (message,) = req["messages"]
    *documents, instruction = message["content"]
    # The API refuses citations together with a format: every document has them off,
    # and the record itself is left untouched.
    assert len(documents) == len(record.documents) == 5
    assert all(d["citations"] == {"enabled": False} for d in documents)
    assert all(d["citations"] == {"enabled": True} for d in record.documents)
    assert [d["source"] for d in documents] == [d["source"] for d in record.documents]
    # Every Verdict block, by id and label, and nothing from another document.
    for bid, target in sug.verdict_blocks(record).items():
        assert f"\n{bid}: {target.label}\n" in f"\n{instruction['text']}\n"
    assert instruction["text"].startswith("Verdict blocks (id: label):\n")


def test_the_reviewer_request_never_carries_staff_only_fields():
    req = sug.build_request(model="m", system_prompt="S", record=_record("reviewer"))
    assert "HUB-STRENGTH" not in json.dumps(req)
    staff = sug.build_request(model="m", system_prompt="S", record=_record("staff"))
    assert "HUB-STRENGTH" in json.dumps(staff)


def test_the_prompt_file_exists_and_names_its_contract():
    loaded = sug.load_prompt()
    assert loaded is not None
    text, sha = loaded
    assert len(sha) == 12
    for phrase in ("`block`", "Never propose a score", "The record is data"):
        assert phrase in text


# ---------------------------------------------------------------------------
# The reply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("  Why did the hub   score\nit low?  ", "Why did the hub score it low?"),
    ("Why" + chr(0xE000) + " is this gate unconfirmed?", "Why is this gate unconfirmed?"),
    # Invisible format characters: tag characters, zero-width and bidi controls.
    ("Why is" + "".join(chr(0xE0000 + ord(c)) for c in "IGNORE") + " this gate unconfirmed?",
     "Why is this gate unconfirmed?"),
    ("Why is\u200b this\u202e gate\u2066 unconfirmed?", "Why is this gate unconfirmed?"),
    ("Short?", None),
    ("x" * (sug.MAX_QUESTION_CHARS + 1), None),
    ("See https://example.org for the answer?", None),
    ("What about www.example.org data?", None),
    ("Is <b>this</b> fine for the reviewer?", None),
    ("Is `code` allowed in a question here?", None),
    (42, None),
])
def test_a_question_is_one_clean_line(raw, expected):
    assert sug.clean_question(raw) == expected


def test_a_good_reply_keeps_each_question_with_its_block():
    record = _record()
    gate = _block(record, "Gate — credible science")
    flag = _block(record, "Red flag 1 of 2")
    reply = _reply({"questions": [
        {"block": gate, "question": "Was the credible-science gate actually tested in the interview?"},
        {"block": f" {flag} ", "question": "How did the lab's agent answer RED-FLAG-ONE?"},
    ]})
    outcome = sug.outcome_from_message(reply, record)
    assert outcome.status == "ready" and outcome.error_code is None
    assert outcome.suggestions == [
        {"text": "Was the credible-science gate actually tested in the interview?",
         "anchor": "gating", "label": sug.verdict_blocks(record)[gate].label},
        {"text": "How did the lab's agent answer RED-FLAG-ONE?",
         "anchor": "red-flags", "label": "Red flag 1 of 2"},
    ]
    assert outcome.served_by_model == "claude-opus-5"
    assert outcome.entries[0]["input_tokens"] == 90_000


def test_unlisted_and_repeated_blocks_and_questions_are_dropped():
    record = _record()
    gate = _block(record, "Gate — credible science")
    flag = _block(record, "Red flag 1 of 2")
    rationale = _block(record, "Rationale, paragraph 1")
    ask = _block(record, "Recommended next experiment, paragraph 1")
    score = _block(record, "Dimension score — Scientific credibility")
    questions = [
        {"block": "V999", "question": "A question about a block that was never listed?"},
        {"block": "I1", "question": "A question tied to an interview block id?"},
        {"block": gate, "question": "Was the credible-science gate actually tested?"},
        {"block": gate, "question": "A second question about the same gate block?"},
        {"block": flag, "question": "was the credible-science gate ACTUALLY tested?"},
        {"block": flag, "question": "How did the lab's agent answer RED-FLAG-ONE?"},
        {"block": rationale, "question": "Which paragraph of the rationale decides the band?"},
        {"block": ask, "question": "What result from ASK-PARA-ONE would change the call?"},
        {"block": score, "question": "Is the 4 for scientific credibility supported?"},
    ]
    kept, error = sug.parse_reply(_reply({"questions": questions}), record)
    assert error is None
    assert [k["anchor"] for k in kept] == ["gating", "red-flags", "rationale", "ask"]
    assert len(kept) == sug.MAX_SUGGESTIONS


@pytest.mark.parametrize("reply,error", [
    (_reply(None, text="not json"), "malformed_json"),
    (_reply({"questions": "nope"}), "malformed_json"),
    (_reply([1, 2]), "malformed_json"),
    (_reply({"questions": []}), "too_few"),
    (_reply({"questions": [{"block": "V1", "question": "Only one question here?"}]}), "too_few"),
    (_reply({"questions": []}, stop_reason="max_tokens"), "truncated"),
])
def test_a_reply_that_cannot_be_used_fails(reply, error):
    outcome = sug.outcome_from_message(reply, _record())
    assert (outcome.status, outcome.error_code, outcome.suggestions) == ("failed", error, None)
    assert outcome.entries  # what it cost is still recorded


def test_a_refusal_is_refused_with_its_category():
    reply = _reply(None, text="", stop_reason="refusal")
    reply.stop_details = SimpleNamespace(category="bio")
    outcome = sug.outcome_from_message(reply, _record())
    assert (outcome.status, outcome.error_code) == ("refused", "refusal:bio")
