"""The authorship emit gate: _reject_ungrounded_authorship + its call sites."""

import pytest

from src.agent.agent import Agent
from src.agent.authorship_rules import LabPublicationRecord
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.unit.test_authorship_rules import (
    CORRECT_ATTRIBUTION,
    DESIDERATA_DOI,
    GOODBOT_INCIDENT,
    WUBOT_ORIGIN,
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # Deterministic empty profiles: no profile-parsed DOIs leak into either
    # lab's "own" set regardless of what happens to be on disk in profiles/.
    # See tests/characterization/test_agent_turn_gm.py::_hermetic_profiles.
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    good = Agent(agent_id="good", bot_name="GoodBot", pi_name="Benjamin Good")
    wu = Agent(agent_id="wu", bot_name="WuBot", pi_name="Chunlei Wu")
    su = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")
    eng = SimulationEngine(agents=[good, wu, su], slack_clients={})
    eng._agent_publications = {
        "wu": LabPublicationRecord(dois={DESIDERATA_DOI}, has_records=True),
        "su": LabPublicationRecord(dois={DESIDERATA_DOI}, has_records=True),
        # good: absent == no records
    }
    return eng


class TestRejectUngroundedAuthorship:
    def test_goodbot_incident_rejected(self, engine):
        reason = engine._reject_ungrounded_authorship(
            engine.agents["good"], GOODBOT_INCIDENT
        )
        assert reason is not None
        assert "no publication records" in reason

    def test_wubot_origin_rejected_via_tagged_lab(self, engine):
        reason = engine._reject_ungrounded_authorship(
            engine.agents["wu"], WUBOT_ORIGIN
        )
        assert reason is not None
        assert "GoodBot" in reason

    def test_correct_attribution_passes(self, engine):
        assert engine._reject_ungrounded_authorship(
            engine.agents["good"], CORRECT_ATTRIBUTION
        ) is None

    def test_wu_claiming_own_paper_with_real_coauthor_passes(self, engine):
        text = (
            "We co-authored *Desiderata* "
            "(https://doi.org/10.1093/bioadv/vbag036) with @SuBot."
        )
        assert engine._reject_ungrounded_authorship(engine.agents["wu"], text) is None

    def test_unknown_tagged_bot_is_ignored(self, engine):
        # A tag that doesn't resolve in the roster must not crash the gate;
        # own-set validation still applies.
        text = (
            "We co-authored *Desiderata* "
            "(https://doi.org/10.1093/bioadv/vbag036) with @NobodyBot."
        )
        assert engine._reject_ungrounded_authorship(engine.agents["wu"], text) is None

    def test_none_text_passes(self, engine):
        # Fail-closed applies to claims, not to the absence of a draft.
        assert engine._reject_ungrounded_authorship(engine.agents["good"], None) is None


class TestProseNamedCoauthors:
    # Audit finding I4: naming the fabricated co-author lab in prose instead
    # of @-tagging it dodged the tagged-lab records check entirely.
    I4_PROSE_PROBE = (
        "Our labs — ours and the Good lab's — actually co-authored the "
        "Desiderata paper (https://doi.org/10.1093/bioadv/vbag036)."
    )

    def test_prose_named_no_records_lab_is_rejected(self, engine):
        reason = engine._reject_ungrounded_authorship(
            engine.agents["wu"], self.I4_PROSE_PROBE
        )
        assert reason is not None
        assert "GoodBot" in reason

    def test_prose_named_lab_with_records_passes(self, engine):
        text = (
            "We co-authored the Desiderata paper with the Su lab "
            "(https://doi.org/10.1093/bioadv/vbag036)."
        )
        assert engine._reject_ungrounded_authorship(engine.agents["wu"], text) is None

    def test_unresolved_prose_lab_name_is_left_alone(self, engine):
        # "Broad" is not in the roster: no crash, no over-blocking — the
        # claim itself is grounded in Wu's own records.
        text = (
            "We co-authored the Desiderata paper with the Broad lab "
            "(https://doi.org/10.1093/bioadv/vbag036)."
        )
        assert engine._reject_ungrounded_authorship(engine.agents["wu"], text) is None

    def test_own_lab_named_in_prose_is_not_a_coauthor(self, engine):
        text = (
            "We co-authored the Desiderata paper here in the Wu lab "
            "(https://doi.org/10.1093/bioadv/vbag036)."
        )
        assert engine._reject_ungrounded_authorship(engine.agents["wu"], text) is None


class TestPostMessageChokepoint:
    async def test_post_message_suppresses_ungrounded_claim(self, engine):
        # No Slack clients and no session factory: if the gate passes, the
        # local-log path would record the message; the gate must return False
        # before that.
        posted = await engine._post_message("good", "general", GOODBOT_INCIDENT)
        assert posted is False
        assert len(engine.message_log) == 0

    async def test_post_message_allows_clean_post(self, engine):
        posted = await engine._post_message("good", "general", CORRECT_ATTRIBUTION)
        assert posted is True
        assert len(engine.message_log) == 1


class TestThreadStateBackoff:
    def test_authorship_reject_count_defaults_to_zero(self):
        t = ThreadState(thread_id="1", channel="general", other_agent_id="wu")
        assert t.authorship_reject_count == 0


class TestPhase4AuthorshipBackoff:
    async def test_two_rejections_back_off_the_thread(self, engine, monkeypatch):
        # Audit finding O-I4(a): the phase-4 gate + backoff is the only
        # loop-breaker for a model that keeps regenerating the same
        # ungrounded reply — pin the real _reply_to_thread block: two
        # rejections drive authorship_reject_count to 2 and flip
        # has_pending_reply False, with nothing ever posted.
        import src.agent.simulation as sim_mod

        good = engine.agents["good"]
        thread = ThreadState(
            thread_id="1700000000.000100",
            channel="general",
            other_agent_id="wu",
            has_pending_reply=True,
        )
        good.state.active_threads[thread.thread_id] = thread

        async def fake_generate(**kwargs):
            return f"<slack_message>{GOODBOT_INCIDENT}</slack_message>"

        monkeypatch.setattr(sim_mod, "generate_with_tools", fake_generate)

        await engine._reply_to_thread(good, thread)
        assert thread.authorship_reject_count == 1
        assert thread.has_pending_reply is True  # retried next turn
        assert len(engine.message_log) == 0

        await engine._reply_to_thread(good, thread)
        assert thread.authorship_reject_count == 2
        assert thread.has_pending_reply is False  # backed off
        assert len(engine.message_log) == 0


class TestPhase5AuthorshipSkip:
    async def test_ungrounded_draft_increments_skip_streak_and_posts_nothing(
        self, engine, monkeypatch
    ):
        # Audit finding O-I4(b): pin the real _phase5_new_post gate block —
        # an ungrounded draft bumps consecutive_phase5_skips and nothing is
        # posted or persisted.
        import src.agent.simulation as sim_mod

        good = engine.agents["good"]
        draft = (
            "```json\n"
            '{"action": "new_post", "channel": "general", "post_type": "paper"}\n'
            "```\n"
            f"<slack_message>\n{GOODBOT_INCIDENT}\n</slack_message>"
        )

        async def fake_generate(**kwargs):
            return draft

        monkeypatch.setattr(sim_mod, "generate_agent_response", fake_generate)
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)

        await engine._phase5_new_post(good)

        assert len(engine.message_log) == 0
        assert good.state.consecutive_phase5_skips == 1


class TestPhase5GateOrdering:
    async def test_gate_runs_before_cohort_tag_strip(self, engine, monkeypatch):
        # Audit finding I1: _strip_disallowed_tags deleted a cohort-disallowed
        # co-author's @tag BEFORE the authorship gate ran, blinding the
        # tagged-co-author check (the only thing catching the WUBOT_ORIGIN
        # shape) to exactly the fabrication it exists to catch. The gate must
        # see the original draft.
        import src.agent.simulation as sim_mod

        wu = engine.agents["wu"]
        # Cohort gate on, empty allowed set → @GoodBot would be stripped.
        wu.allowed_sender_ids = set()

        draft = (
            "```json\n"
            '{"action": "new_post", "channel": "general", "post_type": "paper"}\n'
            "```\n"
            "<slack_message>\n"
            "We co-authored the *Desiderata* paper with @GoodBot — "
            "https://doi.org/10.1093/bioadv/vbag036\n"
            "</slack_message>"
        )

        async def fake_generate(**kwargs):
            return draft

        monkeypatch.setattr(sim_mod, "generate_agent_response", fake_generate)
        # Disable the random phase-5 skip so the draft is always attempted.
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)

        await engine._phase5_new_post(wu)

        assert len(engine.message_log) == 0
        assert wu.state.consecutive_phase5_skips == 1
