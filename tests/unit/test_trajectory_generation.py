"""The Interviews page's trajectory lines, without a database: the request the worker
sends and how a reply is read (src/services/trajectory_generation.py)."""

import json
import subprocess
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.services import trajectory_generation as tg
from src.services.assessment_chat import FALLBACK_BETA
from src.services.interview_posts import Post


def _post(n: int, speaker: str = "LabBot", kind: str = "reply", text: str = "text") -> Post:
    return Post(
        first_row_id=uuid.uuid4(), agent_id=speaker.lower(), speaker=speaker, kind=kind,
        posted_at=float(n), text=text,
    )


def _message(payload=None, *, stop_reason="end_turn", text=None):
    body = text if text is not None else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=body)],
        stop_reason=stop_reason,
        stop_details=None,
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=9_000, output_tokens=300, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, iterations=None,
        ),
    )


@pytest.fixture
def ids():
    return {"P1": _post(1, kind="pitch"), "P2": _post(2, "BlackbirdBot"), "P3": _post(3)}


def test_parse_reply_keeps_valid_lines_and_drops_the_rest(ids):
    reply = _message({"lines": [
        {"post": "P1", "line": "Pitched a pathology reader study on neoadjuvant resections."},
        {"post": "P1", "line": "duplicate"},
        {"post": "P9", "line": "unknown id"},
        {"post": "P2", "line": "see https://doi.org/x"},
        {"post": "P3", "line": "x" * 400},
    ]})
    lines, error = tg.parse_reply(reply, ids)
    assert set(lines) == {"P1", "P3"} and error is None
    assert lines["P1"] == "Pitched a pathology reader study on neoadjuvant resections."
    assert len(lines["P3"]) <= 160 and lines["P3"].endswith("…")


def test_refusal_and_truncation_codes(ids):
    assert tg.parse_reply(_message(stop_reason="refusal"), ids) == ({}, "refusal")
    assert tg.parse_reply(_message(stop_reason="max_tokens"), ids) == ({}, "truncated")


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (_message(text="not json"), "malformed_json"),
        (_message({"lines": "nope"}), "malformed_json"),
        (_message(["a"]), "malformed_json"),
        (_message({"lines": []}), "too_few"),
        (_message({"lines": [{"post": "P9", "line": "unknown id here"}]}), "too_few"),
    ],
)
def test_a_reply_that_cannot_be_used_fails(ids, reply, error):
    assert tg.parse_reply(reply, ids) == ({}, error)


def test_one_surviving_line_is_enough(ids):
    reply = _message({"lines": [{"post": "P2", "line": "Asked which assay supports the claim."}]})
    assert tg.parse_reply(reply, ids) == ({"P2": "Asked which assay supports the claim."}, None)


def test_outcome_stores_lines_by_first_row_id_and_keeps_usage(ids):
    reply = _message({"lines": [{"post": "P2", "line": "Asked which assay supports the claim."}]})
    out = tg.outcome_from_message(reply, ids)
    assert out.status == "ready" and out.error_code is None
    assert out.lines == {str(ids["P2"].first_row_id): "Asked which assay supports the claim."}
    assert out.entries and out.entries[0]["input_tokens"] == 9_000


def test_a_refusal_is_refused_with_its_category(ids):
    reply = _message(stop_reason="refusal")
    reply.stop_details = SimpleNamespace(category="bio")
    out = tg.outcome_from_message(reply, ids)
    assert (out.status, out.error_code, out.lines) == ("refused", "refusal:bio", None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Two   spaces\nand a break ", "Two spaces and a break"),
        ("zero​width", "zerowidth"),
        ("ab", None),
        ("see www.example.org", None),
        ("a `code` span", None),
        ("<b>markup</b> here", None),
        ("IC50 <10 nM in HeLa, >90% inhibition", "IC50 <10 nM in HeLa, >90% inhibition"),
        (None, None),
        (7, None),
    ],
)
def test_a_line_is_one_clean_line(raw, expected):
    assert tg.clean_line(raw) == expected


def test_an_overlong_line_is_clipped_at_the_last_space():
    line = tg.clean_line("word " * 60)
    assert line is not None and line.endswith("word…") and len(line) <= tg.MAX_LINE_CHARS


def test_build_request_sends_posts_as_documents_with_short_ids():
    post1 = _post(1, "LabBot", "pitch", "the pitch")
    post2 = _post(2, "BlackbirdBot", "reply", "the reply")
    req = tg.build_request(model="claude-opus-5", system_prompt="S", posts=[post1, post2])
    content = req["messages"][0]["content"]
    docs = [b for b in content if b["type"] == "document"]
    assert [d["title"] for d in docs] == ["P1 · LabBot · pitch", "P2 · BlackbirdBot · reply"]
    assert [d["source"]["data"] for d in docs] == ["the pitch", "the reply"]
    assert all(d["citations"] == {"enabled": False} for d in docs)
    assert content[-1] == {
        "type": "text", "text": "Posts P1-P2 of one interview, in order. Write one line for each.",
    }
    assert req["max_tokens"] == 8000 and req["max_tokens"] <= 21_333
    assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["system"] == "S"


def test_the_prompt_file_exists_and_is_hashed():
    loaded = tg.load_prompt()
    assert loaded is not None
    text, sha = loaded
    assert "160 characters" in text and len(sha) == 12


def test_the_ceiling_must_be_positive():
    s = Settings(_env_file=None, interview_trajectories_daily_usd_limit=-1.0)
    assert s.interview_trajectories_daily_usd_limit == 10.0
    with pytest.raises(ValueError):
        Settings(_env_file=None, interview_trajectories_daily_usd_limit=float("nan"))


def test_interview_posts_import_chain_avoids_the_rubric():
    """The post helpers the page and the worker share stay free of the two modules that
    parse a ``prompts/`` TOML at import. (The worker as a whole already loads them through
    the chat-suggestions generator's ``assessment_chat`` imports; this keeps the new shared
    module from adding a second path.)"""
    code = (
        "import sys, src.services.interview_posts; "
        "assert 'src.services.blackbird_rubric' not in sys.modules; "
        "assert 'src.services.rubric_revisions' not in sys.modules"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_one_attempt_is_reserved_at_its_own_worst_case():
    # 16,000 input tokens at $5/M plus 8,000 output tokens at $25/M on claude-opus-5.
    assert tg.attempt_reserve("claude-opus-5") == Decimal("0.28")
    assert tg.attempt_reserve("not-a-priced-model") == Decimal("2.50")
    partial = [{"model": "claude-opus-5", "billed": True, "partial": True,
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]
    assert tg.row_cost(partial, "claude-opus-5") == Decimal("0.28")
    assert tg.row_cost(None, "claude-opus-5") == Decimal("0.28")
    assert tg.row_cost([], "claude-opus-5") == Decimal("0")


def test_every_partial_attempt_counts_its_own_reserve():
    entry = {"model": "claude-opus-5", "billed": True, "partial": True,
             "input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    assert tg.row_cost([entry, entry, entry], "claude-opus-5") == 3 * tg.attempt_reserve("claude-opus-5")
    big = {**entry, "reserve_input_tokens": 40_000}
    assert tg.row_cost([big], "claude-opus-5") == tg.attempt_reserve("claude-opus-5", 40_000)
    assert tg.attempt_reserve("claude-opus-5", 40_000) > tg.attempt_reserve("claude-opus-5")


def test_max_tokens_stays_under_the_nonstreaming_ceiling():
    from src.services import llm

    assert tg.MAX_TOKENS <= llm.NONSTREAMING_MAX_TOKENS
