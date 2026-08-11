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
