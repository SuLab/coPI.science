"""Every held interview verdict — pass or fail — posts one headline to the
assessments-summary channel, with no rationale/red-flags/gating content
(design D12/D13/D14/D16)."""
import json

import pytest

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine(monkeypatch, tmp_path):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    hub_client = FakeSlackClient(agent_id="blackbird")
    lab_client = FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": hub_client, "wang": lab_client},
    )
    eng._assessments_summary_channel_id = "C-SUMMARY"
    eng._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = "C-SUMMARY"
    eng._channel_id_map["general"] = "C-GENERAL"
    return eng, hub, lab, hub_client


VERDICT = {
    "subject_agent_id": "wang", "company_or_project": "CRISPR Platform",
    "recommendation": "pass", "funnel_stage": "incubation",
    "scores": {"external_signals": 4, "ip_fto": 4},
}

# The sidecar contract `_extract_assessment_json` parses: BARE JSON inside
# <assessment_json>...</assessment_json>, written OUTSIDE the <slack_message>
# block (see `_ASSESSMENT_RE` and `_capture_hub_assessment`'s docstring).
def _raw(verdict: dict) -> str:
    return "some text <assessment_json>" + json.dumps(verdict) + "</assessment_json>"


async def test_a_held_pass_verdict_posts_a_headline(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="wang")

    await eng._post_assessment_summary(hub, thread, VERDICT, "111.000")

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    text = hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]
    assert "Wang" in text or "wang" in text
    assert "CRISPR Platform" in text
    assert "pass" in text
    assert "rationale" not in text.lower()


async def test_a_held_fail_verdict_also_posts(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t2", channel="general", other_agent_id="wang")
    fail_verdict = {**VERDICT, "recommendation": "no-fit", "scores": {"external_signals": 1}}

    await eng._post_assessment_summary(hub, thread, fail_verdict, "222.000")

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    assert "no-fit" in hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]


async def test_no_scores_still_posts_without_a_band(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t3", channel="general", other_agent_id="wang")
    no_scores = {**VERDICT, "scores": {}}

    await eng._post_assessment_summary(hub, thread, no_scores, "333.000")

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    # An absent `scores` map is "we don't know", not a 0.00 that bands as a
    # decline — `_persist_assessment` leaves weighted_score/band NULL for
    # exactly this case, and the headline must not invent one either.
    text = hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]
    assert "band" not in text.lower()
    assert "score" not in text.lower()


async def test_the_headline_carries_a_permalink_to_the_interview(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t7", channel="general", other_agent_id="wang")

    await eng._post_assessment_summary(hub, thread, VERDICT, "777.000")

    text = hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]
    # FakeSlackClient.get_permalink builds .../archives/{channel_id}/p{ts}, so
    # the SOURCE channel's id (not the summary channel's) must be the one the
    # permalink was asked for.
    assert "C-GENERAL" in text
    assert "View interview" in text


async def test_a_failed_permalink_degrades_instead_of_dropping_the_post(
    monkeypatch, tmp_path,
):
    """D16: a missing permalink is never a reason to skip the post."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t8", channel="general", other_agent_id="wang")

    async def no_link(*a, **kw):
        return None
    monkeypatch.setattr(hub_client, "aget_permalink", no_link)

    await eng._post_assessment_summary(hub, thread, VERDICT, "888.000")

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    assert "link unavailable" in hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]


async def test_a_slack_post_failure_is_swallowed(monkeypatch, tmp_path):
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t4", channel="general", other_agent_id="wang")

    async def boom(*a, **kw):
        raise RuntimeError("Slack is down")
    monkeypatch.setattr(hub_client, "apost_message", boom)

    await eng._post_assessment_summary(hub, thread, VERDICT, "444.000")  # must not raise


async def test_a_permalink_failure_is_swallowed_too(monkeypatch, tmp_path):
    """D16 covers the permalink call as well as the post — and the whole
    helper is inside one try, so a raise there cannot escape either."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t9", channel="general", other_agent_id="wang")

    async def boom(*a, **kw):
        raise RuntimeError("chat.getPermalink exploded")
    monkeypatch.setattr(hub_client, "aget_permalink", boom)

    await eng._post_assessment_summary(hub, thread, VERDICT, "999.000")  # must not raise


async def test_a_model_supplied_project_that_is_not_a_string_never_reaches_slack(
    monkeypatch, tmp_path,
):
    """`company_or_project` comes straight from the model and lands in a
    PUBLIC channel, so a non-string there must degrade to the placeholder
    rather than have its Python repr posted."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t10", channel="general", other_agent_id="wang")
    weird = {**VERDICT, "company_or_project": {"name": "CRISPR Platform"}}

    await eng._post_assessment_summary(hub, thread, weird, "101.000")

    text = hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]
    assert "(untitled)" in text
    assert "{" not in text


async def test_slack_off_posts_nothing(monkeypatch, tmp_path):
    """DB-only mode: `_ensure_assessments_summary_channel` still fills in a
    `local:` channel id, but the transport is a NullTransport with no
    `apost_message`/`aget_permalink` at all. The `is_connected` guard must
    make this a clean no-op rather than an AttributeError per held verdict."""
    from src.agent.transport import NullTransport

    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    eng.slack_clients["blackbird"] = NullTransport(agent_id="blackbird")
    eng._assessments_summary_channel_id = f"local:{ASSESSMENTS_SUMMARY_CHANNEL}"
    thread = ThreadState(thread_id="t11", channel="general", other_agent_id="wang")

    await eng._post_assessment_summary(hub, thread, VERDICT, "111.111")  # must not raise

    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages


async def test_no_summary_channel_posts_nothing(monkeypatch, tmp_path):
    """`_ensure_assessments_summary_channel` returns early (e.g. an incomplete
    Slack channel listing) leaving the id unset — post nothing, raise nothing."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    eng._assessments_summary_channel_id = None
    thread = ThreadState(thread_id="t12", channel="general", other_agent_id="wang")

    await eng._post_assessment_summary(hub, thread, VERDICT, "121.000")

    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages


async def test_capture_hub_assessment_posts_when_the_verdict_is_held(
    monkeypatch, tmp_path,
):
    """D13: the post fires from the real hook point — inside
    `_capture_hub_assessment`'s `if held:` block — for a verdict that was
    actually persisted. `_persist_assessment` is stubbed rather than driven
    against a database because what is under test here is the WIRING, not the
    row (the row is covered by
    tests/integration/test_opportunity_assessment_persistence.py)."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t5h", channel="general", other_agent_id="wang")
    hub.state.active_threads["t5h"] = thread

    persist_calls: list[dict] = []

    async def held(*a, **kw):
        persist_calls.append(kw)
        return True
    monkeypatch.setattr(eng, "_persist_assessment", held)

    await eng._capture_hub_assessment(hub, thread, _raw(VERDICT), "555.000", closes_thread=True)

    # The sidecar really did parse and really did get past `_sidecar_refusal`
    # — without this the assertion below could pass for the wrong reason.
    assert len(persist_calls) == 1
    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    assert "CRISPR Platform" in hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]
    # One interview, one verdict, one headline.
    assert eng._assessed_threads["t5h"].final is True


async def test_capture_hub_assessment_posts_nothing_when_the_verdict_is_not_held(
    monkeypatch, tmp_path,
):
    """A verdict that was NOT held stored nothing and will store nothing (the
    engine has no database), so per D14 there is no assessment for a headline
    to summarise."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t5", channel="general", other_agent_id="wang")
    hub.state.active_threads["t5"] = thread

    # `session_factory` unset is exactly `_persist_assessment`'s real no-DB
    # branch, which returns False after logging a debug line.
    assert eng.session_factory is None

    await eng._capture_hub_assessment(hub, thread, _raw(VERDICT), "555.000", closes_thread=True)

    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages
    # ...and the thread was not marked as holding one either.
    assert "t5" not in eng._assessed_threads


async def test_a_refused_sidecar_never_posts_a_summary(monkeypatch, tmp_path):
    """Design D14: only a HELD OpportunityAssessment row triggers a post. A
    refused/dropped sidecar (recorded as an AssessmentDrop, never persisted
    as a verdict) must not post — structurally guaranteed by
    _capture_hub_assessment's refusal branch returning before it ever calls
    _persist_assessment, but pinned here as an explicit regression test
    rather than left as an inference from code structure."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t6", channel="general", other_agent_id="wang")
    hub.state.active_threads["t6"] = thread
    thread.message_count = 1  # an early, non-concluding, non-closing turn

    drops: list[tuple] = []

    async def fake_record_drop(agent_id, reason, **kw):
        drops.append((agent_id, reason))
    monkeypatch.setattr(eng, "_record_assessment_drop", fake_record_drop)

    async def never(*a, **kw):
        raise AssertionError("_persist_assessment must not be reached")
    monkeypatch.setattr(eng, "_persist_assessment", never)

    # closes_thread=False on an early ordinal is exactly the premature_sidecar
    # refusal case (see CLAUDE.md's "One interview yields exactly one
    # assessment" section) — verified against `_sidecar_refusal`: ordinal=2
    # (message_count + 1) is neither the CONCLUDE ordinal nor a closing reply,
    # so the sidecar is refused before `_persist_assessment` (and therefore
    # `_post_assessment_summary`) is ever reached.
    await eng._capture_hub_assessment(hub, thread, _raw(VERDICT), "666.000", closes_thread=False)

    # Assert the refusal branch is what ran — a `raw` string this test failed
    # to build correctly would otherwise "pass" by parsing to no verdict at all.
    assert drops == [("blackbird", "premature_sidecar")]
    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages
