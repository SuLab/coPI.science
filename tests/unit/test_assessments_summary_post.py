"""Every held interview verdict — pass or fail — posts one headline to the
assessments-summary channel, with no rationale/red-flags/gating content
(design D12/D13/D14/D16)."""
import json

import pytest

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.simulation import SimulationEngine, _HeldVerdict
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
    # A weak smoke check only — `VERDICT` carries no `rationale` key at all, so
    # this would pass even against code that leaked one. D12's real gate is
    # test_the_headline_leaks_no_rationale_red_flags_gating_or_raw_verdict.
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


# Every field D12 forbids the headline from carrying, each filled with a value
# that could only appear in the post by having leaked from this verdict. The
# other tests use `VERDICT`, which has none of these keys at all — so an
# assertion against THAT fixture proves nothing about whether the code would
# leak them if they were present. This one is the real gate.
_LEAK_SENTINELS = (
    "SENTINEL_RATIONALE_TEXT",
    "SENTINEL_FLAG",
    "SENTINEL_GATE_KEY",
    "SENTINEL_RAW",
    "SENTINEL_MILESTONE",
    "SENTINEL_CONFIDENCE",
)

LEAKY_VERDICT = {
    **VERDICT,
    "rationale": "SENTINEL_RATIONALE_TEXT",
    "red_flags": ["SENTINEL_FLAG", "a second SENTINEL_FLAG"],
    "gating": {"SENTINEL_GATE_KEY": "not_met"},
    "raw_verdict": {"anything": "SENTINEL_RAW"},
    "suggested_derisking_milestones": ["SENTINEL_MILESTONE"],
    "confidence": "SENTINEL_CONFIDENCE",
}


async def test_the_headline_leaks_no_rationale_red_flags_gating_or_raw_verdict(
    monkeypatch, tmp_path,
):
    """D12: the summary post is headline-only. A public channel must not show
    more than the manager read-only detail view already shows staff, so none of
    `rationale`, `red_flags`, `gating`, `raw_verdict` (nor the milestones or
    confidence) may reach it — by value OR by field name."""
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t13", channel="general", other_agent_id="wang")

    await eng._post_assessment_summary(hub, thread, LEAKY_VERDICT, "131.000")

    # A post must actually have happened, or "the sentinels are absent" would
    # be vacuously true and this test would guard nothing.
    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    text = hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]

    # The headline still says what it is SUPPOSED to say...
    assert "CRISPR Platform" in text
    assert "pass" in text

    # ...and nothing it is not.
    for sentinel in _LEAK_SENTINELS:
        assert sentinel not in text, f"{sentinel} leaked into the summary post: {text!r}"
    lowered = text.lower()
    for field_name in ("rationale", "red_flag", "gating", "raw_verdict",
                       "milestone", "confidence", "not_met"):
        assert field_name not in lowered, (
            f"the field name {field_name!r} leaked into the summary post: {text!r}"
        )


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


async def test_a_raising_permalink_degrades_instead_of_dropping_the_post(
    monkeypatch, tmp_path,
):
    """D16 again, for the RAISE case rather than the returned-None one.

    `AgentSlackClient.get_permalink` only catches `SlackApiError` itself, so a
    transport-level error (or anything `_call_with_retry` gives up on that is
    not a rate limit) propagates out of it. That must degrade exactly like a
    `None` does — the design says a failed permalink is "not a dropped post".
    Asserting "did not raise" alone would have passed while the headline was
    silently lost to the method-wide except.
    """
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t9", channel="general", other_agent_id="wang")

    async def boom(*a, **kw):
        raise RuntimeError("chat.getPermalink exploded")
    monkeypatch.setattr(hub_client, "aget_permalink", boom)

    await eng._post_assessment_summary(hub, thread, VERDICT, "999.000")  # must not raise

    assert len(hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL]) == 1
    text = hub_client.posted_messages[ASSESSMENTS_SUMMARY_CHANNEL][0]
    assert "link unavailable" in text
    # The verdict itself still made it to the channel — the link was the only
    # casualty.
    assert "CRISPR Platform" in text
    assert "pass" in text


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
    thread.message_count = 1

    drops: list[tuple] = []

    async def fake_record_drop(agent_id, reason, **kw):
        drops.append((agent_id, reason))
    monkeypatch.setattr(eng, "_record_assessment_drop", fake_record_drop)

    async def never(*a, **kw):
        raise AssertionError("_persist_assessment must not be reached")
    monkeypatch.setattr(eng, "_persist_assessment", never)

    # A verdict whose reply CLOSED the interview is terminal, so the thread is
    # done: anything arriving afterwards is a re-capture and is refused. This
    # used to be exercised via `premature_sidecar` (an early, non-concluding
    # turn), but that refusal no longer exists — an early sidecar is now stored
    # as provisional rather than destroyed, because nothing guaranteed the
    # "later turn still owed the verdict" it was being held for. See
    # `_sidecar_refusal`.
    eng._assessed_threads["t6"] = _HeldVerdict(ordinal=1, final=True, slack_ts="1.0")

    await eng._capture_hub_assessment(hub, thread, _raw(VERDICT), "666.000", closes_thread=False)

    # Assert the refusal branch is what ran — a `raw` string this test failed
    # to build correctly would otherwise "pass" by parsing to no verdict at all.
    assert drops == [("blackbird", "duplicate_thread_verdict")]
    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages


@pytest.mark.asyncio
async def test_a_provisional_verdict_is_stored_but_not_announced(monkeypatch, tmp_path):
    """The other half of relaxing the gate, and the reason it is safe.

    An early sidecar is now HELD rather than refused — but a headline is a public
    Slack post that cannot be retracted when a later turn supersedes the row it
    described. So a verdict that does not end the interview is stored for staff
    and stays off the channel until the interview concludes.
    """
    eng, hub, lab, hub_client = _engine(monkeypatch, tmp_path)
    thread = ThreadState(thread_id="t7", channel="general", other_agent_id="wang")
    hub.state.active_threads["t7"] = thread
    thread.message_count = 1  # ordinal 2: neither CONCLUDE nor a closing reply

    persisted: list[dict] = []

    async def fake_persist(agent_id, channel, verdict, **kw):
        persisted.append(verdict)
        return True
    monkeypatch.setattr(eng, "_persist_assessment", fake_persist)

    await eng._capture_hub_assessment(hub, thread, _raw(VERDICT), "777.000", closes_thread=False)

    assert len(persisted) == 1, "an early sidecar must be stored, not destroyed"
    assert ASSESSMENTS_SUMMARY_CHANNEL not in hub_client.posted_messages
    assert eng._assessed_threads["t7"].final is False
