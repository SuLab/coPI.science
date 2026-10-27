"""The research worker's pure parts (src/services/project_research.py): requests, parsing,
screening, the choice schema and the choice checks."""

import json

import pytest

from src.services import llm
from src.services import project_research as pr
from src.services.project_plan import NameEntry

PERSONA = """# Jane Gray

## Research Summary

Studies itch circuits.

## Key Methods and Technologies

- Calcium imaging
- CRISPR screening

## Model Systems

- Mouse models of cholestasis

## Disease Areas / Biological Processes

- Pruritus

## Key Molecular Targets

- MRGPRX4

## Keywords

itch, sensory neurons

## Recent Publications

- A paper (PMID 1)
"""

ITEMS = {
    "Key Methods and Technologies": ["Calcium imaging", "CRISPR screening"],
    "Model Systems": ["Mouse models of cholestasis"],
    "Disease Areas / Biological Processes": ["Pruritus"],
    "Key Molecular Targets": ["MRGPRX4"],
    "Keywords": ["itch", "sensory neurons"],
}


def _lab(**overrides) -> pr.LabContext:
    data = dict(agent_id="gray", pi_name="Jane Gray", persona=PERSONA, items=ITEMS, earlier=[])
    data.update(overrides)
    return pr.LabContext(**data)


def _state() -> dict:
    return pr.new_state(_lab(), prompt_shas={"research": "a" * 12, "choose": "b" * 12})


def _tool_round(n: int) -> list[dict]:
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{n}", "name": "search_pubmed",
             "input": {"query": f"q{n}"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": f"result {n}"},
        ]},
    ]


def test_max_tokens_constants_are_under_the_nonstreaming_ceiling():
    assert pr.RESEARCH_MAX_TOKENS <= llm.NONSTREAMING_MAX_TOKENS
    assert pr.CHOOSE_MAX_TOKENS <= llm.NONSTREAMING_MAX_TOKENS


@pytest.mark.parametrize("domain", pr.RESEARCH_DOMAINS)
def test_persona_domain_sections_for_each_research_domain(domain):
    text = pr.persona_domain_sections(domain)
    assert text is not None
    assert text.startswith("## What you own")
    assert "## What you do not own" in text
    assert "## Answer format" not in text
    assert "verdict_signal" not in text


def test_persona_without_the_heading_is_skipped(tmp_path, monkeypatch):
    broken = tmp_path / "legal.md"
    broken.write_text("# Legal\n\n## What you do not own\n\nx\n\n## You do not decide\n\ny\n")
    real = pr.persona_path

    def fake_path(domain):
        return broken if domain == "legal" else real(domain)

    monkeypatch.setattr(pr, "persona_path", fake_path)
    assert pr.persona_domain_sections("legal") is None
    state = _state()
    statuses = {s["domain"]: s["status"] for s in state["sessions"]}
    assert statuses["legal"] == "skipped"
    assert statuses["clinical"] == "running"


def test_new_state_shape():
    state = _state()
    assert state["version"] == 1
    assert len(state["profile_sha256_12"]) == 12
    assert state["prompt_sha256_12"] == {"research": "a" * 12, "choose": "b" * 12}
    assert [s["domain"] for s in state["sessions"]] == list(pr.RESEARCH_DOMAINS)
    assert state["sessions"][0] == {
        "domain": "clinical", "status": "running", "rounds": 0, "failures": 0,
        "messages": [], "retrieved": {}, "candidates": [], "dropped": [],
    }
    assert state["choice_failures"] == []


def test_research_request_tools_until_the_round_limit():
    state = _state()
    first = pr.research_request(state, 0, _lab(), model="m", frame="FRAME", max_rounds=2)
    assert first["tools"]
    assert first["system"].startswith("FRAME\n\n## Your domain\n\n## What you own")
    assert first["max_tokens"] == pr.RESEARCH_MAX_TOKENS
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default" and first["betas"]
    (opening,) = first["messages"]
    assert opening["content"][0]["text"].endswith("Research now.")

    session = state["sessions"][0]
    session["messages"] = [opening, *_tool_round(1), *_tool_round(2)]
    session["rounds"] = 2
    last = pr.research_request(state, 0, _lab(), model="m", frame="FRAME", max_rounds=2)
    assert "tools" not in last
    final = last["messages"][-1]
    assert final["role"] == "user"
    assert final["content"][-1] == {"type": "text", "text": pr.FINAL_ANSWER_TEXT}
    # Only the request carries the closing text; the stored messages are untouched.
    assert all(
        b.get("text") != pr.FINAL_ANSWER_TEXT for m in session["messages"] for b in m["content"]
    )


def test_research_request_rolls_the_cache_breakpoint_forward():
    state = _state()
    session = state["sessions"][0]
    opening = pr.research_request(state, 0, _lab(), model="m", frame="F", max_rounds=8)[
        "messages"][0]
    session["messages"] = [opening, *_tool_round(1), *_tool_round(2), *_tool_round(3)]
    session["rounds"] = 3
    request = pr.research_request(state, 0, _lab(), model="m", frame="F", max_rounds=8)
    marked = [b for m in request["messages"] for b in m["content"] if "cache_control" in b]
    assert marked == [request["messages"][-1]["content"][-1]]
    assert marked[0]["type"] == "tool_result" and marked[0]["tool_use_id"] == "toolu_3"
    assert not any("cache_control" in b for m in session["messages"] for b in m["content"])


def test_estimate_input_tokens_counts_text_results_and_tool_input():
    request = {
        "system": "s" * 30,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "t" * 30}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "x", "name": "n", "input": {"q": "abc"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "r" * 30},
                {"type": "tool_result", "tool_use_id": "y",
                 "content": [{"type": "text", "text": "u" * 30}]},
            ]},
        ],
    }
    chars = 30 * 4 + len(json.dumps({"q": "abc"}))
    assert pr.estimate_input_tokens(request) == -(-chars // 3)


def _candidate(n: int) -> dict:
    return {
        "use": f"use {n}",
        "anchors": [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}],
        "sources": [{"id": f"PMID:{n}", "shows": "it works"}],
        "why": "because",
    }


def test_parse_session_output_tolerant():
    body = json.dumps({"candidates": [_candidate(1)]})
    assert pr.parse_session_output(f"```json\n{body}\n```")[0]["use"] == "use 1"
    assert pr.parse_session_output(f"Here is my answer:\n{body}\nThanks.")[0]["use"] == "use 1"
    many = json.dumps({"candidates": [_candidate(n) for n in range(1, 6)]})
    parsed = pr.parse_session_output(many)
    assert [c["use"] for c in parsed] == ["use 1", "use 2", "use 3"]
    assert parsed[0] == {
        "use": "use 1",
        "anchors": [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}],
        "sources": [{"id": "PMID:1", "shows": "it works"}],
        "why": "because",
    }
    assert pr.parse_session_output("no json here at all") == []
    assert pr.parse_session_output('{"candidates": "nope"}') == []
    assert pr.parse_session_output("[1, 2]") == []


def test_screen_candidates_drops_unretrieved_sources_and_bad_anchors():
    retrieved = {"pmid:11": {"kind": "pubmed", "title": "Paper eleven"}}
    keeps = {
        "use": "keeps", "why": "w",
        "anchors": [
            {"section": "Key Methods and Technologies", "item": "calcium  imaging."},
            {"section": "Model Systems", "item": "Zebrafish"},
        ],
        "sources": [{"id": "PMID:11", "shows": "s"}, {"id": "PMID:99", "shows": "t"}],
    }
    no_source = {
        "use": "no source", "why": "w",
        "anchors": [{"section": "Model Systems", "item": "Mouse models of cholestasis"}],
        "sources": [{"id": "PMID:99", "shows": "t"}],
    }
    no_method = {
        "use": "no method", "why": "w",
        "anchors": [{"section": "Key Molecular Targets", "item": "MRGPRX4"}],
        "sources": [{"id": "PMID:11", "shows": "s"}],
    }
    kept, dropped = pr.screen_candidates(
        [keeps, no_source, no_method], items=ITEMS, retrieved=retrieved, domain="clinical"
    )
    (only,) = kept
    assert only["domain"] == "clinical"
    assert only["anchors"] == [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}]
    assert [(s["key"], s["kind"], s["title"]) for s in only["sources"]] == [
        ("pmid:11", "pubmed", "Paper eleven")]
    assert only["unretrieved_sources"] == ["PMID:99"]
    assert {d["use"] for d in dropped} == {"no source", "no method"}
    assert all(d["why"] and d["domain"] == "clinical" for d in dropped)


def test_choose_schema_has_no_score_fields():
    def names(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties":
                    yield from value
                yield from names(value)
        elif isinstance(node, list):
            for value in node:
                yield from names(value)

    assert not {"score", "rank", "band", "rating"} & set(names(pr.CHOOSE_SCHEMA))
    assert set(pr.CHOOSE_SCHEMA["required"]) == {
        "chosen", "why_chosen", "others", "title", "summary", "plan", "anchors", "sources"}
    plan = pr.CHOOSE_SCHEMA["properties"]["plan"]
    assert plan["properties"]["lab_tools"]["type"] == "array"
    assert len(plan["required"]) == 10

    def closed(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for value in node.values():
                closed(value)

    closed(pr.CHOOSE_SCHEMA)


# ---------------------------------------------------------------------------
# check_choice
# ---------------------------------------------------------------------------

ROSTER = [
    NameEntry("gray", "GrayBot", "Jane Gray"),
    NameEntry("wilson", "WilsonBot", "Ann Wilson"),
    NameEntry("hub", "BlackbirdBot", "Blackbird Hub"),
]

SURVIVORS = [
    {
        "use": "first use", "why": "w1", "domain": "clinical",
        "anchors": [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}],
        "sources": [{"id": "PMID:11", "shows": "s", "key": "pmid:11", "kind": "pubmed",
                     "title": "Paper eleven"}],
    },
    {
        "use": "second use", "why": "w2", "domain": "legal",
        "anchors": [{"section": "Model Systems", "item": "Mouse models of cholestasis"}],
        "sources": [{"id": "US1", "shows": "s", "key": "patent:US1", "kind": "patent",
                     "title": "A patent"}],
    },
]


def _plan(**overrides) -> dict:
    plan = {
        "indication": "Cholestatic itch", "target": "MRGPRX4", "model": "Mouse",
        "positive_control": "Known agonist", "negative_control": "Vehicle",
        "standard_of_care": "Bile acid sequestrants", "readout": "Scratching bouts",
        "design": "Two groups of ten", "time_and_budget": "9 months, $200K",
        "lab_tools": ["Calcium imaging"],
    }
    plan.update(overrides)
    return plan


def _choice(**overrides) -> dict:
    choice = {
        "chosen": 1, "why_chosen": "Stronger evidence.",
        "others": [{"index": 2, "note": "Weaker patent position."}],
        "title": "MRGPRX4 blockade for cholestatic itch",
        "summary": "Block MRGPRX4 to relieve cholestatic itch.",
        "plan": _plan(), "anchors": ["Calcium imaging"], "sources": ["PMID 11"],
    }
    choice.update(overrides)
    return choice


def _check(choice) -> list[str]:
    return pr.check_choice(
        choice, SURVIVORS, lab=_lab(), roster=ROSTER, hub_agent_ids=frozenset({"hub"})
    )


def test_check_choice_failures():
    assert _check(_choice()) == []
    assert _check(_choice(why_chosen="BlackbirdBot likes it.")) == []

    def one(choice, fragment):
        failures = _check(choice)
        assert any(fragment in f for f in failures), failures

    one(_choice(chosen=3), "not the number of a listed candidate")
    one(_choice(chosen=0), "not the number of a listed candidate")
    one(_choice(others=[{"index": 7, "note": "n"}]), "others names 7")
    one(_choice(anchors=["Mouse models of cholestasis"]), "not one of the chosen candidate's anchors")
    one(_choice(sources=["patent:US1"]), "not one of the chosen candidate's sources")
    one(_choice(summary="As the Wilson lab showed, it works."), "names another lab, PI or bot")
    one(_choice(plan=_plan(design="Ask WilsonBot.")), "names another lab, PI or bot")
    one(_choice(summary="Ping @gray about it."), 'uses "@"')
    one(_choice(summary="See https://example.org for more."), "contains a link")
    one(_choice(summary="See <#C0ABC123|general> for more."), "contains a link")
    one(_choice(summary="Write to <mailto:x.example|the office>."), "contains a link")
    one(_choice(title="t" * 121), "the title is longer than")
    one(_choice(summary="s" * 601), "the summary is longer than")
    assert pr.FIELD_MAX_CHARS == 500  # owner, 2026-10-27 (was 300)
    one(_choice(plan=_plan(readout="r" * 501)), "Readout")
    one(_choice(plan=_plan(lab_tools=["x" * 501])), "an entry is longer than 500")
    assert _check(_choice(plan=_plan(readout="r" * 500, design="d" * 500))) == []
    one(_choice(plan=_plan(lab_tools=[f"tool {n}" for n in range(11)])), "more than 10 lab tools")


def _shown(block: str, label: str) -> list[str]:
    """The ``label`` line's entries exactly as ``_candidates_text`` shows them."""
    line = next(line for line in block.splitlines() if line.strip().startswith(label))
    return line.split(label, 1)[1].strip().split("; ")


@pytest.mark.parametrize("chosen", [1, 2])
def test_check_choice_accepts_references_copied_from_the_candidates_block(chosen):
    """The choice prompt says to copy anchors and sources exactly, and the model copies them
    as the candidates block shows them: ``"<section>: <item>"`` and ``"<id> — <title> (shows:
    …)"`` (dry run 2026-10-26: every lab's choice failed on exactly these strings)."""
    candidate = SURVIVORS[chosen - 1]
    block = pr._candidates_text([candidate])
    choice = _choice(
        chosen=chosen, others=[{"index": 3 - chosen, "note": "n"}],
        anchors=_shown(block, "Builds on:"), sources=_shown(block, "Sources:"),
    )
    assert _check(choice) == []
    stored = pr.resolve_choice(choice, SURVIVORS)
    assert stored["anchors"] == candidate["anchors"]
    assert [s["key"] for s in stored["sources"]] == [s["key"] for s in candidate["sources"]]


def test_shown_references_still_name_only_the_chosen_candidates():
    def one(choice, fragment):
        failures = _check(choice)
        assert any(fragment in f for f in failures), failures

    one(_choice(anchors=["Model Systems: Calcium imaging"]), "not one of the chosen candidate's anchors")
    one(_choice(anchors=["Model Systems: Mouse models of cholestasis"]),
        "not one of the chosen candidate's anchors")
    one(_choice(sources=["US1 — A patent (shows: s)"]), "not one of the chosen candidate's sources")
    assert _check(_choice(sources=["PMID 11 – Paper eleven"])) == []
    assert _check(_choice(sources=["PMID 11 - Paper eleven"])) == []


def test_check_choice_fails_a_posting_that_cannot_fit(monkeypatch):
    def too_long(**kwargs):
        raise ValueError("posting too long")

    monkeypatch.setattr(pr, "assemble_posting", too_long)
    assert _check(_choice()) == [
        "the posting is too long for 2 Slack messages; shorten the summary or the plan"
    ]


def test_check_choice_passes_a_posting_longer_than_one_slack_message():
    """The re-check of 2026-10-27 failed two labs only on "too long for one Slack message";
    a posting may now take two (owner's choice)."""
    long_summary = "Block MRGPRX4 to relieve cholestatic itch. " * 13
    plan = _plan(
        **{key: "x " * 140 for key, _label in pr.TEMPLATE_FIELDS if key != "lab_tools"},
        lab_tools=["y " * 140] * 4,
    )
    choice = _choice(summary=long_summary[:600], plan=plan)
    stored = pr.resolve_choice(choice, SURVIVORS)
    assert len(pr.posting_for(stored, _lab())) > 4000
    assert _check(choice) == []


def test_check_choice_names_a_link_or_at_in_the_posting(monkeypatch):
    def carries_link(**kwargs):
        raise ValueError("posting carries a link or an @")

    monkeypatch.setattr(pr, "assemble_posting", carries_link)
    assert _check(_choice()) == ["the posting would carry a link or an @"]


def test_resolve_choice_stores_names_not_indices():
    stored = pr.resolve_choice(_choice(), SURVIVORS)
    assert stored["chosen"] == {"use": "first use", "why": "w1", "domain": "clinical"}
    assert stored["others"] == [
        {"use": "second use", "domain": "legal", "note": "Weaker patent position."}]
    assert stored["anchors"] == [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}]
    assert stored["sources"] == [{
        "key": "pmid:11", "kind": "pubmed", "title": "Paper eleven", "domain": "clinical",
        "shows": "s",
    }]
    assert set(stored["plan"]) == {
        "indication", "target", "model", "positive_control", "negative_control",
        "standard_of_care", "readout", "design", "time_and_budget", "lab_tools"}


def test_ledger_row_cost_prices_reserves_and_usage():
    model = "claude-opus-5"
    reserve = pr._call_reserve(model, "research:clinical")
    assert pr.ledger_row_cost(None, model, "research:clinical") == reserve
    assert pr.ledger_row_cost([], model, "choose") == 0
    partial = [{"model": model, "billed": True, "partial": True, "reserve_input_tokens": 0,
                "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0}]
    assert pr.ledger_row_cost(partial, model, "research:clinical") == reserve
    real = [{"model": model, "billed": True, "input_tokens": 1000, "output_tokens": 100,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]
    assert 0 < pr.ledger_row_cost(real, model, "choose") < reserve
