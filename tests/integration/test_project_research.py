"""Project research end to end (src/services/project_research.py): one model call per slot,
the stored research state, the ledger and its ceiling, the choice and the posting, and the
dry-run script (scripts/projects_dry_run.py)."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from sqlalchemy import func, select, update

from scripts import projects_dry_run as dry
from src.agent.role_capabilities import hub_role_names
from src.config import get_settings
from src.models import AssessmentReview
from src.models.hub_project import HubProject, HubProjectCall, HubProjectClock
from src.services import llm, profile_export, project_search
from src.services import project_research as pr
from src.services.trajectory_generation import _in_flight_entry
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.fakes import FakeAnthropic, _Message, _TextBlock, _ToolUseBlock, _Usage

pytestmark = pytest.mark.integration

MODEL = "claude-opus-5"
PRIVATE_MARKER = "PRIVATE-PROFILE-MARKER-7d1e"
TRANSCRIPT_MARKER = "TRANSCRIPT-MARKER-91c2"
STAFF_REVIEW_MARKER = "STAFF-REVIEW-MARKER-7f3a"

PERSONA = """# Jane Gray

## Research Summary

Studies itch circuits in sensory neurons.

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

- An itch paper (PMID 1)
"""

SEVEN_SECTION_TEXTS = (
    "Studies itch circuits in sensory neurons.", "- Calcium imaging",
    "- Mouse models of cholestasis", "- Pruritus", "- MRGPRX4", "itch, sensory neurons",
    "- An itch paper (PMID 1)",
)


@pytest.fixture
def factory(db_session):
    """The worker's session factory, routed to the test's rolled-back session."""

    @asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


@pytest.fixture(autouse=True)
def switches(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "project_research_enabled", True)
    monkeypatch.setattr(settings, "llm_project_research_model", MODEL)
    monkeypatch.setattr(settings, "project_research_daily_usd_limit", 25.0)
    monkeypatch.setattr(settings, "project_research_max_tool_rounds", 8)
    pr._WARNED.clear()
    return settings


@pytest.fixture
async def world(db_session, monkeypatch, tmp_path):
    """A lab with a public persona and a private profile, a hub and another lab, a live run
    with a clock at 7,200 s, and one ``requested`` project for the lab."""
    n = uuid.uuid4().hex[:8]
    lab_id, hub_id, other_id = f"gray{n}", f"hub{n}", f"wilson{n}"
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / f"{lab_id}.md").write_text(PERSONA, encoding="utf-8")
    monkeypatch.setattr(profile_export, "PROFILES_DIR", personas)

    user = await factories.make_user(db_session, name="Jane Gray")
    await factories.make_profile(db_session, user=user, private_profile_md=PRIVATE_MARKER)
    await factories.make_agent(
        db_session, user=user, agent_id=lab_id, bot_name=f"GrayBot{n}", pi_name="Jane Gray"
    )
    await factories.make_agent(
        db_session, agent_id=hub_id, bot_name="BlackbirdBot", pi_name="Blackbird Hub",
        role=hub_role_names()[0],
    )
    await factories.make_agent(
        db_session, agent_id=other_id, bot_name=f"WilsonBot{n}", pi_name="Ann Wilson"
    )
    run = await factories.make_simulation_run(db_session)
    db_session.add(HubProjectClock(simulation_run_id=run.id, run_seconds=7200.0))
    project = HubProject(
        simulation_run_id=run.id, agent_id=lab_id, slot=1, status="requested",
        requested_run_seconds=0.0,
    )
    db_session.add(project)
    await db_session.flush()

    async def live(db, **kw):
        return run.id

    monkeypatch.setattr(pr, "live_run_id", live)
    return SimpleNamespace(lab_id=lab_id, hub_id=hub_id, run=run, project_id=project.id)


@pytest.fixture
def install(monkeypatch):
    """``install(*replies) -> FakeAnthropic``: the client every call goes through."""

    def _install(*responses):
        client = FakeAnthropic(list(responses))
        monkeypatch.setattr(llm, "get_anthropic_client", lambda: client)
        return client

    return _install


@pytest.fixture
def tools(monkeypatch):
    """Every research tool call; each returns one PubMed source, ``pmid:11``."""
    box = SimpleNamespace(calls=[], error=None, during=None)

    async def fake(name, tool_input):
        box.calls.append((name, dict(tool_input)))
        if box.during is not None:
            await box.during()
        if box.error is not None:
            raise box.error
        return project_search.ToolResult(
            text=f"{name} results",
            sources=({"key": "pmid:11", "kind": "pubmed", "title": "Paper eleven"},),
        )

    monkeypatch.setattr(project_search, "execute_research_tool", fake)
    return box


def _msg(blocks, stop="end_turn"):
    message = _Message(
        content=list(blocks), stop_reason=stop,
        usage=_Usage(input_tokens=2_000, output_tokens=300),
    )
    message.model = MODEL
    return message


def _tool_reply(*names):
    blocks = [_TextBlock(text="Searching.")]
    blocks += [
        _ToolUseBlock(id=f"toolu_{i}", name=name, input={"query": "itch"})
        for i, name in enumerate(names, 1)
    ]
    return _msg(blocks, stop="tool_use")


def _answer(candidates):
    return _msg([_TextBlock(text=json.dumps({"candidates": candidates}))])


def _status_error(code: int):
    request = httpx.Request("POST", "https://api.invalid/v1/messages")
    error = anthropic.APIStatusError(
        f"status {code}", response=httpx.Response(code, request=request), body=None,
    )

    def raise_it(kwargs):
        raise error

    return raise_it


async def _project(db_session, project_id) -> HubProject:
    return await db_session.get(HubProject, project_id, populate_existing=True)


async def _calls(db_session, project_id) -> list[HubProjectCall]:
    result = await db_session.execute(
        select(HubProjectCall)
        .where(HubProjectCall.project_id == project_id)
        .order_by(HubProjectCall.created_at, HubProjectCall.id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


def _kept(use, domain, *, why="w"):
    return {
        "use": use, "why": why, "domain": domain,
        "anchors": [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}],
        "sources": [{"id": "PMID:11", "shows": "it works", "key": "pmid:11", "kind": "pubmed",
                     "title": "Paper eleven"}],
        "dropped_anchors": [], "unretrieved_sources": [],
    }


async def _seed_research(db_session, world, survivors_by_domain=None, *, dropped=None):
    """Put the project in ``researching`` with every session done."""
    lab = pr.build_lab_context(world.lab_id, "Jane Gray", [])
    state = pr.new_state(lab, prompt_shas={"research": "a" * 12, "choose": "b" * 12})
    for session in state["sessions"]:
        session["status"] = "done"
        session["candidates"] = list((survivors_by_domain or {}).get(session["domain"], []))
        session["dropped"] = list((dropped or {}).get(session["domain"], []))
    await db_session.execute(
        update(HubProject).where(HubProject.id == world.project_id)
        .values(status="researching", research=state)
    )
    await db_session.flush()
    return state


def _plan(**overrides):
    plan = {
        "indication": "Cholestatic itch", "target": "MRGPRX4", "model": "Mouse",
        "positive_control": "Known agonist", "negative_control": "Vehicle",
        "standard_of_care": "Bile acid sequestrants", "readout": "Scratching bouts",
        "design": "Two groups of ten mice", "time_and_budget": "9 months, $200K",
        "lab_tools": ["Calcium imaging"],
    }
    plan.update(overrides)
    return plan


def _choice_reply(**overrides):
    choice = {
        "chosen": 1, "why_chosen": "The strongest evidence.",
        "others": [{"index": 2, "note": "A weaker patent position."}],
        "title": "MRGPRX4 blockade for cholestatic itch",
        "summary": "Block MRGPRX4 to relieve cholestatic itch.",
        "plan": _plan(), "anchors": ["Calcium imaging"], "sources": ["PMID 11"],
    }
    choice.update(overrides)
    return _msg([_TextBlock(text=json.dumps(choice))])


def _request_text(request) -> str:
    return "\n".join(
        block.get("text", "") for message in request["messages"]
        for block in message["content"] if isinstance(block, dict)
    )


def _seconds_out(moment: datetime) -> float:
    return (moment - datetime.now(UTC)).total_seconds()


# ---------------------------------------------------------------------------
# Research sessions
# ---------------------------------------------------------------------------


async def test_one_model_call_per_slot_and_state_advances(db_session, factory, world, install, tools):
    fake = install(_tool_reply("search_pubmed", "search_trials"))

    assert await pr.research_projects(factory) is True
    assert len(fake.calls) == 1
    assert fake.options_calls == [{"timeout": pr.CALL_TIMEOUT_SECONDS, "max_retries": 0}]
    assert [name for name, _ in tools.calls] == ["search_pubmed", "search_trials"]

    project = await _project(db_session, world.project_id)
    assert project.status == "researching"
    session = project.research["sessions"][0]
    assert (session["domain"], session["status"], session["rounds"]) == ("clinical", "running", 1)
    opening, assistant, results = session["messages"]
    assert opening["role"] == "user" and opening["content"][0]["text"].endswith("Research now.")
    assert [b["type"] for b in assistant["content"]] == ["text", "tool_use", "tool_use"]
    assert [(b["type"], b["tool_use_id"]) for b in results["content"]] == [
        ("tool_result", "toolu_1"), ("tool_result", "toolu_2")]
    assert session["retrieved"] == {"pmid:11": {"kind": "pubmed", "title": "Paper eleven"}}

    (call,) = await _calls(db_session, world.project_id)
    assert (call.step, call.status, call.model) == ("research:clinical", "done", MODEL)
    assert call.usage_by_model[0]["input_tokens"] == 2_000
    assert call.served_by_model == MODEL


async def test_session_ends_on_a_final_answer_and_screens_candidates(
    db_session, factory, world, install, tools
):
    good = {
        "use": "MRGPRX4 antagonists for cholestatic itch",
        "anchors": [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}],
        "sources": [{"id": "PMID:11", "shows": "a"}, {"id": "PMID:99", "shows": "b"}],
        "why": "Strong genetics.",
    }
    unsourced = {
        "use": "Something unsourced",
        "anchors": [{"section": "Model Systems", "item": "Mouse models of cholestasis"}],
        "sources": [{"id": "PMID:99", "shows": "b"}],
        "why": "Hunch.",
    }
    fake = install(_tool_reply("search_pubmed"), _answer([good, unsourced]))

    assert await pr.research_projects(factory) is True
    assert await pr.research_projects(factory) is True
    assert len(fake.calls) == 2

    project = await _project(db_session, world.project_id)
    assert project.status == "researching"
    clinical, scientific = project.research["sessions"][:2]
    assert clinical["status"] == "done" and clinical["messages"] == []
    (kept,) = clinical["candidates"]
    assert kept["domain"] == "clinical"
    assert [s["key"] for s in kept["sources"]] == ["pmid:11"]
    assert kept["unretrieved_sources"] == ["PMID:99"]
    (dropped,) = clinical["dropped"]
    assert dropped["use"] == "Something unsourced" and dropped["why"]
    assert (scientific["status"], scientific["rounds"]) == ("running", 0)


async def test_cache_breakpoint_rolls_forward(db_session, factory, world, install, tools):
    fake = install(*(_tool_reply("search_pubmed") for _ in range(3)), _tool_reply("search_pubmed"))
    for _ in range(4):
        assert await pr.research_projects(factory) is True

    round_three = fake.calls[3]
    marked = [
        block for message in round_three["messages"] for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]
    assert marked == [round_three["messages"][-1]["content"][-1]]
    assert marked[0]["type"] == "tool_result"
    project = await _project(db_session, world.project_id)
    stored = project.research["sessions"][0]["messages"]
    assert not any("cache_control" in b for m in stored for b in m["content"])


async def test_research_inputs_are_the_public_profile_only_and_fenced(
    db_session, factory, world, install, tools
):
    await factories.make_agent_message(
        db_session, run=world.run, agent_id=world.lab_id, content=TRANSCRIPT_MARKER,
        message_ts="1700000000.000100", sender_name="GrayBot",
    )
    # A staff review of the lab's interview: research reads the public profile only.
    seeded = await seed_interview(
        db_session, run=world.run, subject=world.lab_id, channel="review-marker-channel",
    )
    db_session.add(AssessmentReview(
        assessment_id=seeded.assessment_id, reviewer_user_id=None, reviewer_name="Staff",
        score=2, comment=STAFF_REVIEW_MARKER, feedback_mode="log_only",
    ))
    await db_session.flush()
    fake = install(_tool_reply("search_pubmed"))
    assert await pr.research_projects(factory) is True

    request = fake.calls[0]
    text = _request_text(request)
    assert text.count("<lab_profile>") == 7
    for body in SEVEN_SECTION_TEXTS:
        assert f"<lab_profile>\n{body}" in text
    everything = json.dumps(request, default=str)
    assert PRIVATE_MARKER not in everything
    assert TRANSCRIPT_MARKER not in everything
    assert STAFF_REVIEW_MARKER not in everything


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_transient_error_does_not_count_and_waits(db_session, factory, world, install, tools):
    fake = install(_status_error(529))

    assert await pr.research_projects(factory) is True
    project = await _project(db_session, world.project_id)
    session = project.research["sessions"][0]
    assert (session["status"], session["failures"]) == ("running", 0)
    assert 100 < _seconds_out(project.next_step_after) <= 125
    (call,) = await _calls(db_session, world.project_id)
    assert (call.status, call.error_code, call.usage_by_model) == ("failed", "api_status_529", [])

    assert await pr.research_projects(factory) is False  # waiting
    assert len(fake.calls) == 1


async def test_three_failures_end_the_session(db_session, factory, world, install, tools):
    install(_status_error(400), _status_error(400), _status_error(400))
    for expected in (1, 2):
        assert await pr.research_projects(factory) is True
        project = await _project(db_session, world.project_id)
        assert project.research["sessions"][0]["failures"] == expected
        assert 590 < _seconds_out(project.next_step_after) <= 605
        await db_session.execute(
            update(HubProject).where(HubProject.id == world.project_id)
            .values(next_step_after=None)
        )
    assert await pr.research_projects(factory) is True

    project = await _project(db_session, world.project_id)
    session = project.research["sessions"][0]
    assert (session["status"], session["failures"], session["candidates"]) == ("failed", 3, [])
    assert project.next_step_after is None
    assert [c.status for c in await _calls(db_session, world.project_id)] == ["failed"] * 3


async def test_a_non_sdk_error_is_a_counted_failure_not_a_stuck_claim(
    db_session, factory, world, install, tools
):
    # An exception that is not an anthropic.AnthropicError (here an SDK rejecting a
    # parameter) must still settle the claimed ledger row and make the project wait, or every
    # sweep would book another reserve.
    def type_error(kwargs):
        raise TypeError("unexpected keyword argument 'fallbacks'")

    install(type_error)
    assert await pr.research_projects(factory) is True
    project = await _project(db_session, world.project_id)
    assert project.research["sessions"][0]["failures"] == 1
    assert 590 < _seconds_out(project.next_step_after) <= 605
    (call,) = await _calls(db_session, world.project_id)
    assert (call.status, call.error_code) == ("failed", "error:TypeError")
    assert [e.get("partial") for e in call.usage_by_model] == [True]
    assert await pr.research_projects(factory) is False  # waiting, no second reserve


async def test_refusal_ends_the_session_at_once(db_session, factory, world, install, tools):
    refusal = _msg([], stop="refusal")
    refusal.stop_details = SimpleNamespace(category="bio")
    fake = install(refusal, _tool_reply("search_pubmed"))

    assert await pr.research_projects(factory) is True
    project = await _project(db_session, world.project_id)
    assert project.research["sessions"][0]["status"] == "refused"
    (call,) = await _calls(db_session, world.project_id)
    assert (call.status, call.error_code) == ("refused", "refusal:bio")

    assert await pr.research_projects(factory) is True  # the next session's turn
    assert fake.calls[1]["system"]  # a second request was made
    project = await _project(db_session, world.project_id)
    assert project.research["sessions"][1]["rounds"] == 1


async def test_pubmed_unavailable_waits_without_counting(db_session, factory, world, install, tools):
    install(_tool_reply("search_pubmed"))
    tools.error = project_search.PubMedUnavailable("PubMed is down")

    assert await pr.research_projects(factory) is True
    project = await _project(db_session, world.project_id)
    session = project.research["sessions"][0]
    assert (session["failures"], session["rounds"], session["messages"]) == (0, 0, [])
    assert session["retrieved"] == {}
    assert 590 < _seconds_out(project.next_step_after) <= 605
    (call,) = await _calls(db_session, world.project_id)
    assert (call.status, call.error_code) == ("failed", "pubmed_unavailable")
    assert call.usage_by_model[0]["input_tokens"] == 2_000  # the call itself was billed


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


async def test_ceiling_stops_new_calls(db_session, factory, world, install, tools):
    db_session.add(HubProjectCall(
        project_id=world.project_id, step="research:clinical", model=MODEL, status="done",
        usage_by_model=[{"model": MODEL, "billed": True, "input_tokens": 50_000_000,
                         "output_tokens": 0, "cache_read_input_tokens": 0,
                         "cache_creation_input_tokens": 0}],
    ))
    await db_session.flush()
    assert await pr.spend_24h(db_session) >= Decimal("25")
    fake = install()

    assert await pr.research_projects(factory) is False
    assert fake.calls == []
    assert (await _project(db_session, world.project_id)).status == "requested"


async def test_a_claimed_call_that_never_finished_counts_and_the_step_reruns(
    db_session, factory, world, install, tools
):
    lab = pr.build_lab_context(world.lab_id, "Jane Gray", [])
    prompts = pr.load_prompts()
    state = pr.new_state(lab, prompt_shas=prompts.shas)
    await db_session.execute(
        update(HubProject).where(HubProject.id == world.project_id)
        .values(status="researching", research=state)
    )
    db_session.add(HubProjectCall(
        project_id=world.project_id, step="research:clinical", model=MODEL,
        status="in_progress", usage_by_model=[_in_flight_entry(MODEL, 20_000)],
    ))
    await db_session.flush()
    assert await pr.spend_24h(db_session) == pr._call_reserve(MODEL, "research:clinical", 20_000)

    fake = install(_tool_reply("search_pubmed"))
    assert await pr.research_projects(factory) is True
    expected = pr.research_request(
        state, 0, lab, model=MODEL, frame=prompts.research, max_rounds=8
    )
    assert fake.calls[0]["messages"] == expected["messages"]
    assert fake.calls[0]["tools"] == expected["tools"]
    calls = await _calls(db_session, world.project_id)
    assert [c.status for c in calls].count("in_progress") == 1  # the dead one still counts


async def test_only_the_live_runs_projects_are_researched(
    db_session, factory, world, install, tools, monkeypatch
):
    other = await factories.make_simulation_run(db_session)

    async def live(db, **kw):
        return other.id

    monkeypatch.setattr(pr, "live_run_id", live)
    fake = install(_tool_reply("search_pubmed"))
    assert await pr.research_projects(factory) is False

    async def none(db, **kw):
        return None

    monkeypatch.setattr(pr, "live_run_id", none)
    assert await pr.research_projects(factory) is False
    assert fake.calls == []
    assert (await _project(db_session, world.project_id)).status == "requested"


async def test_a_project_the_engine_ended_is_not_overwritten(
    db_session, factory, world, install, tools
):
    async def engine_ends_it():
        await db_session.execute(
            update(HubProject).where(HubProject.id == world.project_id)
            .values(status="not_finished", reason_code="lab_not_live")
        )

    tools.during = engine_ends_it
    install(_tool_reply("search_pubmed"))

    assert await pr.research_projects(factory) is True
    project = await _project(db_session, world.project_id)
    assert (project.status, project.reason_code) == ("not_finished", "lab_not_live")
    assert project.research["sessions"][0]["rounds"] == 0  # the claim's state, not the result's
    (call,) = await _calls(db_session, world.project_id)
    assert call.status == "done"  # the paid call is still recorded


# ---------------------------------------------------------------------------
# Ends without a call
# ---------------------------------------------------------------------------


async def test_no_survivors_ends_not_finished_no_idea(db_session, factory, world, install, tools):
    await _seed_research(
        db_session, world,
        dropped={"clinical": [{"use": "x", "why": "no source that a search in this session returned"}]},
    )
    fake = install()

    assert await pr.research_projects(factory) is False
    assert fake.calls == []
    project = await _project(db_session, world.project_id)
    assert (project.status, project.reason_code) == ("not_finished", "no_idea")
    assert project.research["sessions"][0]["dropped"][0]["why"]


async def test_worker_ended_projects_record_finished_run_seconds(
    db_session, factory, world, install, tools
):
    (profile_export.PROFILES_DIR / f"{world.lab_id}.md").unlink()  # no persona to research
    install()

    assert await pr.research_projects(factory) is False
    project = await _project(db_session, world.project_id)
    assert (project.status, project.reason_code) == ("not_finished", "research_failed")
    assert project.finished_run_seconds == 7200.0
    assert project.finished_at is not None


# ---------------------------------------------------------------------------
# The choice
# ---------------------------------------------------------------------------


def _ints(node):
    if isinstance(node, bool):
        return []
    if isinstance(node, int):
        return [node]
    if isinstance(node, dict):
        return [i for value in node.values() for i in _ints(value)]
    if isinstance(node, list):
        return [i for value in node for i in _ints(value)]
    return []


async def test_choice_writes_ready_to_post_with_the_assembled_posting(
    db_session, factory, world, install, tools
):
    await _seed_research(db_session, world, {
        "clinical": [_kept("MRGPRX4 antagonists for cholestatic itch", "clinical")],
        "legal": [_kept("A licensing angle", "legal")],
    })
    fake = install(_choice_reply())

    assert await pr.research_projects(factory) is True
    request = fake.calls[0]
    assert request["output_config"]["format"]["schema"] == pr.CHOOSE_SCHEMA
    assert "## Blackbird's criteria" in _request_text(request)

    project = await _project(db_session, world.project_id)
    assert project.status == "ready_to_post"
    assert project.title == "MRGPRX4 blockade for cholestatic itch"
    assert project.posting_text.startswith(":test_tube: *Blackbird project for the Gray lab*")
    choice = project.choice
    assert choice["chosen"] == {
        "use": "MRGPRX4 antagonists for cholestatic itch", "why": "w", "domain": "clinical"}
    assert choice["others"] == [
        {"use": "A licensing angle", "domain": "legal", "note": "A weaker patent position."}]
    assert choice["anchors"] == [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}]
    assert choice["sources"] == [{
        "key": "pmid:11", "kind": "pubmed", "title": "Paper eleven", "domain": "clinical",
        "shows": "it works",
    }]
    assert choice["plan"]["lab_tools"] == ["Calcium imaging"]
    assert _ints(choice) == []
    (call,) = await _calls(db_session, world.project_id)
    assert (call.step, call.status) == ("choose", "done")


async def test_survivors_are_numbered_from_one_in_domain_order(
    db_session, factory, world, install, tools
):
    await _seed_research(db_session, world, {
        "legal": [_kept("legal one", "legal")],
        "clinical": [_kept("clinical one", "clinical"), _kept("clinical two", "clinical")],
        "commercial": [_kept("commercial one", "commercial")],
    })
    fake = install(_choice_reply(chosen=3, others=[]))

    assert await pr.research_projects(factory) is True
    text = _request_text(fake.calls[0])
    order = [
        "1. From the clinical specialist: clinical one",
        "2. From the clinical specialist: clinical two",
        "3. From the commercial specialist: commercial one",
        "4. From the legal specialist: legal one",
    ]
    positions = [text.index(line) for line in order]
    assert positions == sorted(positions)
    project = await _project(db_session, world.project_id)
    assert project.choice["chosen"]["use"] == "commercial one"


async def test_a_failed_choice_retries_once_then_ends_choice_failed(
    db_session, factory, world, install, tools
):
    await _seed_research(db_session, world, {
        "clinical": [_kept("first", "clinical")], "legal": [_kept("second", "legal")],
    })
    bad = "As the Wilson lab showed, blocking MRGPRX4 relieves itch."
    fake = install(_choice_reply(summary=bad), _choice_reply(summary=bad))

    assert await pr.research_projects(factory) is True
    project = await _project(db_session, world.project_id)
    assert project.status == "researching"
    (failures,) = project.research["choice_failures"]
    assert any("Wilson" in f for f in failures)

    assert await pr.research_projects(factory) is True
    retry = _request_text(fake.calls[1])
    assert "## Your previous answer failed these checks" in retry
    assert "names another lab, PI or bot" in retry
    project = await _project(db_session, world.project_id)
    assert (project.status, project.reason_code) == ("not_finished", "choice_failed")
    assert project.finished_run_seconds == 7200.0
    assert len(project.research["choice_failures"]) == 2


async def test_choice_with_a_link_fails_its_checks(db_session, factory, world, install, tools):
    await _seed_research(db_session, world, {
        "clinical": [_kept("first", "clinical")], "legal": [_kept("second", "legal")],
    })
    install(_choice_reply(summary="Details at www.example.org about MRGPRX4."))

    assert await pr.research_projects(factory) is True
    project = await _project(db_session, world.project_id)
    assert project.status == "researching"
    assert "contains a link" in project.research["choice_failures"][0]
    assert project.posting_text is None


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


async def _row_counts(db_session) -> tuple[int, int]:
    projects = await db_session.scalar(select(func.count()).select_from(HubProject))
    calls = await db_session.scalar(select(func.count()).select_from(HubProjectCall))
    return projects, calls


async def test_dry_run_writes_no_rows_and_refuses_a_path_inside_the_repository(
    db_session, factory, world, install, tools, tmp_path
):
    fake = install()
    with pytest.raises(ValueError, match="inside the repository"):
        await dry.dry_run(
            factory, labs=[world.lab_id], max_usd=Decimal("40"),
            out=Path(dry.REPO_ROOT) / "dry-run.json",
        )
    assert fake.calls == []

    before = await _row_counts(db_session)
    out = tmp_path / "dry-run.json"
    report = await dry.dry_run(factory, labs=[world.lab_id], max_usd=Decimal("40"), out=out)
    assert await _row_counts(db_session) == before
    assert (await _project(db_session, world.project_id)).status == "requested"
    assert len(fake.calls) == len(pr.RESEARCH_DOMAINS)  # each session answered "OK"
    assert report["labs"][0]["outcome"] == "no_idea"
    assert json.loads(out.read_text())["labs"][0]["agent_id"] == world.lab_id


async def test_dry_run_file_shape(db_session, factory, world, install, tools, tmp_path):
    candidate = {
        "use": "MRGPRX4 antagonists for cholestatic itch",
        "anchors": [{"section": "Key Methods and Technologies", "item": "Calcium imaging"}],
        "sources": [{"id": "PMID:11", "shows": "it works"}],
        "why": "Strong genetics.",
    }
    install(
        _tool_reply("search_pubmed"), _answer([candidate]), "OK", "OK", "OK", "OK",
        _choice_reply(others=[]),
    )
    out = tmp_path / "dry-run.json"
    await dry.dry_run(factory, labs=[world.lab_id], max_usd=Decimal("40"), out=out)

    report = json.loads(out.read_text())
    assert set(report) == {"generated_at", "model", "total_usd", "labs"}
    assert report["model"] == MODEL and Decimal(report["total_usd"]) > 0
    (lab,) = report["labs"]
    assert set(lab) == {
        "agent_id", "pi_name", "cost_usd", "sessions", "choice_failures", "outcome",
        "choice", "title", "posting_text",
    }
    assert lab["outcome"] == "ready_to_post"
    assert lab["title"] == "MRGPRX4 blockade for cholestatic itch"
    assert lab["posting_text"].startswith(":test_tube: *Blackbird project for the Gray lab*")
    assert [set(s) for s in lab["sessions"]] == [
        {"domain", "status", "candidates", "dropped"}] * len(pr.RESEARCH_DOMAINS)
    assert lab["choice"]["chosen"]["domain"] == "clinical"


def test_dry_run_refuses_the_container_checkout():
    with pytest.raises(ValueError):
        dry.check_out_path(Path("/app/out.json"))
    assert dry.check_out_path(Path("/tmp/elsewhere/out.json")) == Path("/tmp/elsewhere/out.json").resolve()
