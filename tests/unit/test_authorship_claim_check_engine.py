"""The authorship-claim check in the engine (the 2026-10-15 publication-handling design
§11 R6.10 to R6.12): the posting chokepoint in log and enforce mode, the hub, panel notes,
the thread root, the rows, blocked replies, blocked pitches and the feedback the next draft
reads. A DB-less engine from the prompt-freeze fixtures; ``claim_check.record_check`` is
patched to collect its keyword arguments, except where a test needs the real writer."""
import logging
import types
import uuid
from unittest.mock import AsyncMock

import pytest

from src.agent import claim_check
from src.agent.engine.constants import ABANDONED_OUTCOME
from src.agent.message_log import LogEntry
from src.agent.publication_records import PublicationIds
from src.config import get_settings
from tests.characterization.freeze.fixtures import (
    INTERVIEW_CHANNEL,
    SEED_BASE,
    apply_frozen_settings,
    build_engine,
    install_profiles,
    seed_thread,
)

B = "10.1093/bioinformatics/btad570"
T = f"{SEED_BASE + 600}.000000"
NO_RECORDS = ("first-person authorship claim, but this lab has no publication records to "
              "verify against (fail closed)")
UNCITED = ("first-person authorship claim without a DOI or PubMed link attached to it — "
           "unverifiable (cite the paper's DOI from your publication list)")
CLAIM = "Our 2023 Nature paper showed this."
ENVELOPE = ('```json\n{"action": "new_post", "channel": "general", "post_type": "pitch", '
            '"tagged_agent": "blackbird"}\n```\n<slack_message>{body}</slack_message>')
PITCH = ENVELOPE.replace("{body}", "@BlackbirdBot Our 2023 Nature paper showed the effect.")
CLEAN_PITCH = ENVELOPE.replace("{body}", "@BlackbirdBot Isogenic panels could screen this target.")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """``world(mode, patch_record=True)`` -> engine, agents, clients, collected rows."""
    def make(mode: str = "log", *, patch_record: bool = True):
        apply_frozen_settings(monkeypatch)
        monkeypatch.setenv("AUTHORSHIP_CLAIM_CHECK_MODE", mode)
        get_settings.cache_clear()
        root = install_profiles(tmp_path, monkeypatch)
        eng, agents, clients = build_engine()
        rows: list[dict] = []
        if patch_record:
            async def _collect(session_factory, **kwargs):
                rows.append(kwargs)
            monkeypatch.setattr(claim_check, "record_check", _collect)
        return types.SimpleNamespace(eng=eng, agents=agents, clients=clients, rows=rows, root=root)
    yield make
    get_settings.cache_clear()


def _without_records(w, agent):
    """No rows and a persona with no identifier: an empty C12 record (has_records False)."""
    (w.root / "public" / f"{agent.agent_id}.md").write_text(
        f"# {agent.pi_name} Lab\n\nNo publications listed.\n", encoding="utf-8")
    agent.reload_profiles()
    agent.set_publication_ids(PublicationIds())


def _holds_b(agent):
    agent.set_publication_ids(PublicationIds(in_use_dois=frozenset({B}), row_count=1))


def _lab_thread(w):
    """Thread T: root by wang, one hub reply; wang replies next."""
    return seed_thread(w.eng, replier=w.agents["lab"], partner=w.agents["hub"], thread_id=T,
                       prior=2, root_by_replier=True)


# --- R6.10 -------------------------------------------------------------------------------

async def test_log_mode_posts_a_rejected_lab_reply_unchanged_and_records_it(world):
    w = world("log")
    _without_records(w, w.agents["lab"])
    _lab_thread(w)
    assert await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T)
    assert w.clients["wang"].posted[-1]["text"] == CLAIM
    assert len(w.rows) == 1
    row = w.rows[0]
    assert (row["mode"], row["outcome"], row["phase"], row["thread_key"], row["agent_id"]) == (
        "log", "rejected", "thread_reply", T, "wang")
    assert row["reasons"][0].code == "no_records" and row["excerpt"] == CLAIM


async def test_enforce_mode_blocks_a_rejected_lab_reply(world):
    w = world("enforce")
    _without_records(w, w.agents["lab"])
    _lab_thread(w)
    before = len(w.eng.message_log.get_thread_history(T))
    assert await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T) is None
    assert w.clients["wang"].posted == []
    assert len(w.eng.message_log.get_thread_history(T)) == before
    assert w.eng.ctx.claim_feedback.get("wang", T) == NO_RECORDS
    assert w.rows[0]["mode"] == "enforce"


async def test_hub_reply_is_logged_not_blocked_in_enforce_mode(world):
    w = world("enforce")
    seed_thread(w.eng, replier=w.agents["hub"], partner=w.agents["lab"], thread_id=T, prior=3)
    text = "We published https://doi.org/10.1/x."
    assert await w.eng._post_message("blackbird", INTERVIEW_CHANNEL, text, thread_ts=T)
    assert (w.rows[0]["mode"], w.rows[0]["outcome"]) == ("log", "rejected")
    assert w.eng.ctx.claim_feedback.get("blackbird", T) is None


async def test_hub_concluding_reply_keeps_its_verdict_in_enforce_mode(world, monkeypatch):
    w = world("enforce")
    hub = w.agents["hub"]
    thread = seed_thread(w.eng, replier=hub, partner=w.agents["lab"], thread_id=T, prior=11)
    captured = []

    async def _capture(agent, thread_, raw, posted, **_kw):
        captured.append(posted)

    monkeypatch.setattr(w.eng.verdicts, "_capture_hub_assessment", _capture)
    monkeypatch.setattr(w.eng.reply_lane, "_check_thread_outcome", AsyncMock())
    raw = ("<slack_message>We published https://doi.org/10.1/x. That concludes the screen."
           "</slack_message>\n<assessment_json>{\"recommendation\": \"advance\"}</assessment_json>")
    await w.eng._handle_reply_response(hub, thread, raw, [], [])
    assert captured and captured[0]
    assert w.rows[0]["mode"] == "log"


async def test_panel_notes_are_not_checked(world):
    w = world("enforce")
    seed_thread(w.eng, replier=w.agents["hub"], partner=w.agents["lab"], thread_id=T, prior=3)
    await w.eng._post_panel_note(
        "blackbird", channel=INTERVIEW_CHANNEL, thread_ts=T, domain="clinical",
        question="We published 10.1/x?", verdict_signal="gap",
    )
    assert w.clients["blackbird"].posted, "the note was posted"
    assert w.rows == []


async def test_the_check_sees_tags_before_the_cohort_strip(world):
    w = world("log")
    lab = w.agents["lab"]
    lab.allowed_sender_ids = {"wang", "blackbird"}
    _holds_b(lab)
    _without_records(w, w.agents["other"])
    _lab_thread(w)
    await w.eng._post_message("wang", INTERVIEW_CHANNEL,
                              f"We co-authored https://doi.org/{B} with @GordyBot.", thread_ts=T)
    reason = w.rows[0]["reasons"][0]
    assert (w.rows[0]["outcome"], reason.code) == ("rejected", "partner_no_records")
    assert "@GordyBot" in reason.text


async def test_thread_root_is_the_context(world):
    w = world("log")
    _holds_b(w.agents["lab"])
    _lab_thread(w)
    w.eng.message_log.get_entry(T).content = f"Pitch — our engine (https://doi.org/{B}) screens cohorts."
    await w.eng._post_message(
        "wang", INTERVIEW_CHANNEL,
        "Plainly: yes, that paper is ours — Dr. Wang is an author on it.", thread_ts=T)
    assert w.rows[0]["outcome"] == "grounded"


async def test_split_root_chunks_join_the_context(world):
    w = world("log")
    _holds_b(w.agents["lab"])
    _lab_thread(w)
    w.eng.message_log.get_entry(T).content = "Pitch — part one, no citation."
    w.eng.message_log.append(LogEntry(
        ts=f"{SEED_BASE + 600}.500000", channel=INTERVIEW_CHANNEL, sender_agent_id="wang",
        sender_name="WangBot", content=f"Part two: https://doi.org/{B}", thread_ts=T,
        posted_at=SEED_BASE + 600.5, is_bot=True,
    ))
    await w.eng._post_message(
        "wang", INTERVIEW_CHANNEL,
        "Plainly: yes, that paper is ours — Dr. Wang is an author on it.", thread_ts=T)
    assert w.rows[0]["outcome"] == "grounded"


async def test_no_claim_and_grounded_are_recorded_too(world):
    w = world("log")
    _holds_b(w.agents["lab"])
    _lab_thread(w)
    await w.eng._post_message("wang", INTERVIEW_CHANNEL, "Three isogenic lines.", thread_ts=T)
    await w.eng._post_message("wang", INTERVIEW_CHANNEL, f"We published https://doi.org/{B}.",
                              thread_ts=T)
    assert [r["outcome"] for r in w.rows] == ["no_claim", "grounded"]


async def test_record_write_failure_neither_blocks_nor_raises(world, caplog):
    w = world("log", patch_record=False)
    _lab_thread(w)

    class _Raising:
        def __call__(self):
            raise RuntimeError("database down")

    w.eng.ctx.session_factory = _Raising()
    w.eng.ctx.simulation_run_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="src.agent.claim_check"):
        assert await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T)
    assert "Authorship claim check row not recorded for wang: RuntimeError: database down" in caplog.text


async def test_db_less_engine_writes_no_rows(world):
    w = world("log", patch_record=False)
    _lab_thread(w)
    assert w.eng.ctx.session_factory is None
    assert await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T)


# --- R6.11 -------------------------------------------------------------------------------

async def test_a_block_counts_as_a_suppression(world):
    w = world("enforce")
    lab = w.agents["lab"]
    _without_records(w, lab)
    thread = _lab_thread(w)
    await w.eng._handle_reply_response(lab, thread, f"<slack_message>{CLAIM}</slack_message>", [], [])
    assert thread.suppressed_post_count == 1
    assert thread.has_pending_reply is True
    assert T in lab.state.active_threads


async def test_a_second_block_abandons_the_thread(world, monkeypatch):
    w = world("enforce")
    lab = w.agents["lab"]
    _without_records(w, lab)
    thread = _lab_thread(w)
    outcomes = []
    real_close = w.eng.threads._close_thread

    async def _spy(agent, thread_, outcome, *args, **kwargs):
        outcomes.append(outcome)
        await real_close(agent, thread_, outcome, *args, **kwargs)

    monkeypatch.setattr(w.eng.threads, "_close_thread", _spy)
    for _ in range(2):
        await w.eng._handle_reply_response(lab, thread, f"<slack_message>{CLAIM}</slack_message>", [], [])
    assert thread.suppressed_post_count == 2
    assert outcomes == [ABANDONED_OUTCOME]
    assert w.eng.ctx.claim_feedback.get("wang", T) is None


async def test_the_next_reply_prompt_carries_the_reason(world, monkeypatch):
    w = world("enforce")
    lab = w.agents["lab"]
    _without_records(w, lab)
    thread = _lab_thread(w)
    await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T)
    seen = []

    async def _generate(agent, thread_, system_prompt, messages, *rest):
        seen.append(messages)
        return "<slack_message>Three isogenic lines.</slack_message>"

    monkeypatch.setattr(w.eng.reply_lane, "_generate_reply", _generate)
    await w.eng._reply_to_thread(lab, thread)
    assert seen[0][-1]["content"].endswith(
        "\n\nYour previous draft was not posted: first-person authorship claim, but this lab has "
        "no publication records to verify against (fail closed). Cite the DOI or PubMed link "
        "from your Recent Publications section for any paper you describe as your lab's, or "
        "rephrase without claiming authorship.")


async def test_feedback_is_cleared_on_post(world):
    w = world("enforce")
    _without_records(w, w.agents["lab"])
    _lab_thread(w)
    await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T)
    assert w.eng.ctx.claim_feedback.get("wang", T) == NO_RECORDS
    assert await w.eng._post_message("wang", INTERVIEW_CHANNEL, "Three isogenic lines.", thread_ts=T)
    assert w.eng.ctx.claim_feedback.get("wang", T) is None


async def test_close_clears_feedback_for_both_participants(world):
    w = world("enforce")
    thread = _lab_thread(w)
    w.eng.ctx.claim_feedback.note("wang", T, "a")
    w.eng.ctx.claim_feedback.note("blackbird", T, "b")
    await w.eng._close_thread(w.agents["lab"], thread, "no_proposal")
    assert w.eng.ctx.claim_feedback.get("wang", T) is None
    assert w.eng.ctx.claim_feedback.get("blackbird", T) is None


async def test_log_mode_never_stores_feedback(world, monkeypatch):
    w = world("log")
    lab = w.agents["lab"]
    _without_records(w, lab)
    thread = _lab_thread(w)
    await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T)
    assert w.eng.ctx.claim_feedback.get("wang", T) is None
    seen = []

    async def _generate(agent, thread_, system_prompt, messages, *rest):
        seen.append(messages)
        return "<slack_message>Three isogenic lines.</slack_message>"

    monkeypatch.setattr(w.eng.reply_lane, "_generate_reply", _generate)
    await w.eng._reply_to_thread(lab, thread)
    assert "Your previous draft was not posted" not in seen[0][-1]["content"]


@pytest.mark.parametrize("mode", ["log", "enforce"])
async def test_a_rules_fault_posts_unchecked_with_a_warning(world, monkeypatch, caplog, mode):
    # Audit defect 2: the rules raise; in either mode the post still goes out unchanged.
    from src.agent.engine import slack_io

    def boom(*args, **kwargs):
        raise RuntimeError("rules bug")

    w = world(mode)
    _without_records(w, w.agents["lab"])
    _lab_thread(w)
    monkeypatch.setattr(slack_io, "check_draft_authorship", boom)
    assert await w.eng._post_message("wang", INTERVIEW_CHANNEL, CLAIM, thread_ts=T)
    assert w.clients["wang"].posted[-1]["text"] == CLAIM
    assert w.rows == []
    assert "Authorship claim check failed; posting unchecked" in caplog.text


async def test_prompt_builders_without_feedback_are_unchanged(world):
    w = world("log")
    lab = w.agents["lab"]
    thread = _lab_thread(w)
    history = [{"sender": "WangBot", "content": "root"}, {"sender": "BlackbirdBot", "content": "q"}]
    args = (thread, history, "BlackbirdBot", "Blackbird")
    assert lab.build_phase4_prompt(*args) == lab.build_phase4_prompt(*args, authorship_feedback=None)
    system, messages = lab.build_phase4_prompt(*args, authorship_feedback="F")
    assert system == lab.build_phase4_prompt(*args)[0]
    assert messages[-1]["content"].endswith("\n\nF")
    assert lab.build_phase5_prompt() == lab.build_phase5_prompt(authorship_feedback=None)
    assert lab.build_phase5_prompt(authorship_feedback="F")[1][-1]["content"].endswith("\n\nF")


# --- R6.12 -------------------------------------------------------------------------------
# wang keeps the fixture persona (two DOIs), so its record is non-empty and the reason is
# `uncited`, as the spec's expectation states ("no records" there means no stored rows).

def _pitch_world(world, mode="enforce"):
    w = world(mode)
    lab, hub = w.agents["lab"], w.agents["hub"]
    lab.allowed_sender_ids = {"wang", "blackbird"}
    hub.allowed_sender_ids = {"wang", "blackbird", "gordy"}
    available = w.eng._available_post_types(lab)
    assert w.eng._post_type_rejection(lab, "pitch", "blackbird", available) is None
    return w, lab, available


async def test_a_blocked_pitch_restores_the_skip_streak_plus_one(world):
    w, lab, available = _pitch_world(world)
    lab.state.consecutive_phase5_skips = 2
    await w.eng._handle_phase5_response(lab, PITCH, [], "call-1", available)
    assert lab.state.consecutive_phase5_skips == 3


async def test_a_blocked_pitch_stores_phase5_feedback_and_counts_nothing(world):
    w, lab, available = _pitch_world(world)
    await w.eng._handle_phase5_response(lab, PITCH, [], "call-1", available)
    assert w.eng.ctx.claim_feedback.get("wang", "phase5") == UNCITED
    assert w.eng._proposals_posted == 0
    assert lab.message_count == 0
    assert w.clients["wang"].posted == []


async def test_the_next_pitch_prompt_carries_the_reason(world, monkeypatch):
    w, lab, available = _pitch_world(world)
    await w.eng._handle_phase5_response(lab, PITCH, [], "call-1", available)
    seen = []

    async def _generate(**kwargs):
        seen.append(kwargs["messages"])
        return CLEAN_PITCH

    monkeypatch.setattr("src.agent.engine.deps.generate_agent_response", _generate)
    await w.eng._phase5_new_post(lab)
    assert seen[0][-1]["content"].endswith("\n\n" + claim_check.feedback_text(UNCITED))


async def test_a_posted_pitch_clears_phase5_feedback(world):
    w, lab, available = _pitch_world(world)
    w.eng.ctx.claim_feedback.note("wang", "phase5", "earlier reason")
    await w.eng._handle_phase5_response(lab, CLEAN_PITCH, [], "call-1", available)
    assert w.eng.ctx.claim_feedback.get("wang", "phase5") is None
    assert w.eng._proposals_posted == 1


async def test_a_non_gate_suppression_keeps_todays_handling(world, monkeypatch, caplog):
    w, lab, available = _pitch_world(world)

    async def _suppressed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(w.eng.slack_io, "_post_message", _suppressed)
    with caplog.at_level(logging.INFO, logger="src.agent.simulation"):
        await w.eng._handle_phase5_response(lab, PITCH, [], "call-1", available)
    assert lab.state.consecutive_phase5_skips == 0
    assert w.eng.ctx.claim_feedback.get("wang", "phase5") is None
    assert "suppressed — not counted, nothing persisted" in caplog.text
