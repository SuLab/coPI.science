"""Phase 2 scan under the unreviewed-proposal block, and the catch-up on unblock.

A blocked agent's Phase 5 acts only on funding and collab_private posts, so
Phase 2 must scan only those while the block holds, then — once it lifts —
scan the regular posts it skipped, bounded by the catch-up window and cap.
"""

import time

import pytest

import src.agent.simulation as sim_mod
from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ProposalRef
from src.config import get_settings
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

PRIVATE = "collab-su-wu"


class _FakeLLM:
    def __init__(self):
        self.calls: list[dict] = []
        # Responses to hand out before falling back to an empty selection.
        self.queue: list[str] = []
        # Prompts containing this text get an empty reply, like a refusal.
        self.refuse_if: str | None = None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse_if and self.refuse_if in kwargs["messages"][0]["content"]:
            return ""
        if self.queue:
            return self.queue.pop(0)
        return '{"selected_post_ids": []}'

    def prompts(self) -> str:
        return "\n".join(c["messages"][0]["content"] for c in self.calls)

    def prompt(self, i: int = -1) -> str:
        return self.calls[i]["messages"][0]["content"]


def _make_engine(tmp_path, monkeypatch, agent_id="su"):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    agent = Agent(agent_id, f"{agent_id.title()}Bot", "Some PI")
    engine = SimulationEngine(agents=[agent], slack_clients={})
    agent.state.subscribed_channels = {"general", "funding-opportunities", PRIVATE}
    engine._channel_visibility[PRIVATE] = VISIBILITY_COLLAB_PRIVATE
    llm = _FakeLLM()
    monkeypatch.setattr(sim_mod, "generate_agent_response", llm)
    return engine, agent, llm


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return _make_engine(tmp_path, monkeypatch)


def _post(engine, posted_at, channel, content):
    engine.message_log.append(LogEntry(
        ts=f"{posted_at:.6f}", channel=channel, sender_agent_id="wu",
        sender_name="WuBot", content=content, posted_at=posted_at, is_bot=True,
    ))


def _block(agent, n=2):
    agent.state.pending_proposals = [
        ProposalRef(thread_id=f"proposal-{i}", channel="general",
                    other_agent_id="wu", summary_text="s", proposed_at=0.0)
        for i in range(n)
    ]


def _unblock(agent):
    for p in agent.state.pending_proposals:
        p.reviewed = True


def _end_turn(engine, agent):
    # _run_turn's cursor update, without running the other phases.
    agent.state.last_seen_cursor = engine.message_log.latest_timestamp


class TestBlockedScan:
    async def test_scans_only_funding_and_private_posts(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        _post(engine, now - 3600, "general", "REGULAR-POST")
        _post(engine, now - 3500, "funding-opportunities", ":moneybag: FUNDING-POST")
        _post(engine, now - 3400, PRIVATE, "PRIVATE-POST")

        await engine._phase2_scan_filter(agent)

        assert len(llm.calls) == 1
        prompt = llm.prompt()
        assert "FUNDING-POST" in prompt
        assert "PRIVATE-POST" in prompt
        assert "REGULAR-POST" not in prompt
        assert agent.state.scan_regular_skipped_since == now - 7200

    async def test_only_regular_posts_means_no_llm_call(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        _post(engine, now - 3600, "general", "REGULAR-POST")

        await engine._phase2_scan_filter(agent)

        assert llm.calls == []

    async def test_a_private_post_is_recognised_by_its_persisted_visibility(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        agent.state.subscribed_channels.add("collab-unmapped")
        _block(agent)
        engine.message_log.append(LogEntry(
            ts=f"{now - 60:.6f}", channel="collab-unmapped", sender_agent_id="wu",
            sender_name="WuBot", content="UNMAPPED-PRIVATE", posted_at=now - 60,
            visibility=VISIBILITY_COLLAB_PRIVATE,
        ))

        await engine._phase2_scan_filter(agent)

        assert "UNMAPPED-PRIVATE" in llm.prompt()

    async def test_exempt_agent_is_not_gated(self, tmp_path, monkeypatch):
        engine, agent, llm = _make_engine(tmp_path, monkeypatch, agent_id="schultz")
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        _post(engine, now - 3600, "general", "REGULAR-POST")

        await engine._phase2_scan_filter(agent)

        assert "REGULAR-POST" in llm.prompt()
        assert agent.state.scan_regular_skipped_since is None

    async def test_setting_off_scans_every_post(self, setup, monkeypatch):
        engine, agent, llm = setup
        monkeypatch.setattr(get_settings(), "scan_skip_regular_posts_when_blocked", False)
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        _post(engine, now - 3600, "general", "REGULAR-POST")

        await engine._phase2_scan_filter(agent)

        assert "REGULAR-POST" in llm.prompt()
        assert agent.state.scan_regular_skipped_since is None


class TestCatchUpOnUnblock:
    async def test_skipped_regular_posts_are_scanned_once_after_unblock(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        _post(engine, now - 3600, "general", "SKIPPED-REGULAR")
        _post(engine, now - 3500, "funding-opportunities", ":moneybag: SEEN-FUNDING")
        await engine._phase2_scan_filter(agent)
        _end_turn(engine, agent)

        _post(engine, now - 100, "general", "FRESH-REGULAR")
        _unblock(agent)
        blocked_calls = len(llm.calls)
        await engine._phase2_scan_filter(agent)

        prompt = "\n".join(
            c["messages"][0]["content"] for c in llm.calls[blocked_calls:]
        )
        assert "SKIPPED-REGULAR" in prompt
        assert "FRESH-REGULAR" in prompt
        assert "SEEN-FUNDING" not in prompt, "already scanned while blocked"
        assert agent.state.scan_regular_skipped_since is None

        _end_turn(engine, agent)
        calls_before = len(llm.calls)
        await engine._phase2_scan_filter(agent)
        assert len(llm.calls) == calls_before, "the catch-up must not repeat"

    async def test_posts_older_than_the_window_are_not_caught_up(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 40 * 3600
        _block(agent)
        _post(engine, now - 30 * 3600, "general", "TOO-OLD")
        _post(engine, now - 3600, "general", "RECENT")
        await engine._phase2_scan_filter(agent)
        _end_turn(engine, agent)
        assert llm.calls == []

        _unblock(agent)
        await engine._phase2_scan_filter(agent)

        prompt = llm.prompt()
        assert "RECENT" in prompt
        assert "TOO-OLD" not in prompt

    async def test_only_the_newest_max_posts_are_caught_up(self, setup, monkeypatch):
        engine, agent, llm = setup
        monkeypatch.setattr(get_settings(), "scan_catchup_max_posts", 2)
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        for i in range(4):
            _post(engine, now - 3600 + i, "general", f"SKIPPED-{i}")
        await engine._phase2_scan_filter(agent)
        _end_turn(engine, agent)

        _unblock(agent)
        await engine._phase2_scan_filter(agent)

        prompt = llm.prompt()
        assert "SKIPPED-2" in prompt and "SKIPPED-3" in prompt
        assert "SKIPPED-0" not in prompt and "SKIPPED-1" not in prompt

    async def test_never_blocked_agent_scans_only_past_its_cursor(self, setup):
        engine, agent, llm = setup
        now = time.time()
        _post(engine, now - 3600, "general", "BEFORE-CURSOR")
        agent.state.last_seen_cursor = now - 1800
        _post(engine, now - 60, "general", "AFTER-CURSOR")

        await engine._phase2_scan_filter(agent)

        prompt = llm.prompt()
        assert "AFTER-CURSOR" in prompt
        assert "BEFORE-CURSOR" not in prompt


class TestCatchUpRobustness:
    async def test_marker_survives_several_blocked_turns(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        for i in range(3):
            _post(engine, now - 3600 + 600 * i, "general", f"BLOCKED-TURN-{i}")
            await engine._phase2_scan_filter(agent)
            _end_turn(engine, agent)
            assert agent.state.scan_regular_skipped_since == now - 7200
        assert llm.calls == []

        _unblock(agent)
        await engine._phase2_scan_filter(agent)

        prompts = llm.prompts()
        for i in range(3):
            assert f"BLOCKED-TURN-{i}" in prompts

    async def test_a_refused_batch_loses_only_its_own_posts(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        # Seven skipped posts -> batches of 5 and 2 (oldest first).
        _post(engine, now - 3600, "general", "TRIGGER-REFUSAL")
        for i in range(1, 7):
            _post(engine, now - 3600 + i, "general", f"SKIPPED-{i}")
        await engine._phase2_scan_filter(agent)
        _end_turn(engine, agent)

        _unblock(agent)
        llm.refuse_if = "TRIGGER-REFUSAL"
        await engine._phase2_scan_filter(agent)

        assert len(llm.calls) == 2
        assert "SKIPPED-5" in llm.prompt(1) and "SKIPPED-6" in llm.prompt(1)
        assert "TRIGGER-REFUSAL" not in llm.prompt(1)
        assert agent.state.scan_regular_skipped_since is None

        _end_turn(engine, agent)
        await engine._phase2_scan_filter(agent)
        assert len(llm.calls) == 2, "a refused batch is not retried"

    async def test_cursor_rewound_below_the_marker_falls_back_to_a_normal_scan(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 3600
        _block(agent)
        await engine._phase2_scan_filter(agent)
        _post(engine, now - 1800, "general", "AFTER-REWIND")
        # e.g. _rewind_cursors_for_private_channels moving the cursor back
        agent.state.last_seen_cursor = now - 7200

        _unblock(agent)
        await engine._phase2_scan_filter(agent)

        assert len(llm.calls) == 1
        assert "AFTER-REWIND" in llm.prompt()
        assert agent.state.scan_regular_skipped_since is None

    async def test_catch_up_with_nothing_to_scan_retires_the_marker(self, setup):
        engine, agent, llm = setup
        now = time.time()
        agent.state.last_seen_cursor = now - 7200
        _block(agent)
        await engine._phase2_scan_filter(agent)
        _post(engine, now - 3600, "funding-opportunities", ":moneybag: SEEN-FUNDING")
        await engine._phase2_scan_filter(agent)
        _end_turn(engine, agent)
        calls_before = len(llm.calls)

        _unblock(agent)
        await engine._phase2_scan_filter(agent)

        assert len(llm.calls) == calls_before
        assert agent.state.scan_regular_skipped_since is None

    def test_rebuild_seeds_the_marker_only_for_blocked_agents(self, setup):
        engine, agent, _ = setup
        engine._seed_scan_skip_marker(agent)
        assert agent.state.scan_regular_skipped_since is None

        _block(agent)
        engine._seed_scan_skip_marker(agent)
        assert agent.state.scan_regular_skipped_since == 0.0


class TestPhase5UsesTheSharedPredicate:
    async def test_blocked_agent_does_not_act_on_a_regular_post(self, setup, monkeypatch):
        from src.agent.state import PostRef

        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        now = time.time()
        _post(engine, now - 60, "general", "REGULAR-INTERESTING")
        agent.state.interesting_posts = [PostRef(
            post_id=f"{now - 60:.6f}", channel="general", sender_agent_id="wu",
            content_snippet="REGULAR-INTERESTING", posted_at=now - 60,
        )]

        _block(agent)
        await engine._phase5_new_post(agent)
        assert llm.calls == [], "blocked: Phase 5 must not consider a regular post"

        _unblock(agent)
        llm.queue = ['{"action": "skip"}']
        await engine._phase5_new_post(agent)
        assert len(llm.calls) == 1
        assert "REGULAR-INTERESTING" in llm.prompt()


class TestPhase5PiPriorityUnderBlock:
    """A PI-priority post in a public channel must not buy a blocked agent an
    Opus call: the post-LLM block check would reject any reply to it."""

    def _pi_post(self, engine, channel, content):
        from src.agent.state import PostRef

        now = time.time()
        _post(engine, now - 60, channel, content)
        return PostRef(
            post_id=f"{now - 60:.6f}", channel=channel, sender_agent_id="wu",
            content_snippet=content, posted_at=now - 60, pi_priority=True,
        )

    async def test_public_pi_priority_post_waits_for_the_block(self, setup, monkeypatch):
        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        agent.state.interesting_posts = [self._pi_post(engine, "general", "PI-TAGGED-PUBLIC")]

        _block(agent)
        await engine._phase5_new_post(agent)
        assert llm.calls == []
        assert [p.pi_priority for p in agent.state.interesting_posts] == [True], (
            "the PI-priority post must be kept for after the block lifts"
        )

        _unblock(agent)
        llm.queue = ['{"action": "skip"}']
        await engine._phase5_new_post(agent)
        assert len(llm.calls) == 1
        assert "PI-TAGGED-PUBLIC" in llm.prompt()

    async def test_private_pi_priority_post_still_bypasses_the_block(self, setup, monkeypatch):
        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        agent.state.interesting_posts = [self._pi_post(engine, PRIVATE, "PI-PRIVATE-HANDOVER")]
        _block(agent)
        llm.queue = ['{"action": "skip"}']

        await engine._phase5_new_post(agent)

        assert len(llm.calls) == 1
        assert "PI-PRIVATE-HANDOVER" in llm.prompt()


class TestBlockPredicate:
    def test_reviewed_and_funding_proposals_do_not_count(self, setup):
        engine, agent, _ = setup
        _post(engine, 1000.0, "funding-opportunities", ":moneybag: funding root")
        funding_root = f"{1000.0:.6f}"
        agent.state.pending_proposals = [
            ProposalRef(thread_id=funding_root, channel="funding-opportunities",
                        other_agent_id="wu", summary_text="s", proposed_at=0.0),
            ProposalRef(thread_id="regular-1", channel="general",
                        other_agent_id="wu", summary_text="s", proposed_at=0.0),
            ProposalRef(thread_id="regular-2", channel="general",
                        other_agent_id="wu", summary_text="s", proposed_at=0.0,
                        reviewed=True),
        ]
        assert engine._unreviewed_proposal_block_active(agent) is False

        agent.state.pending_proposals[2].reviewed = False
        assert engine._unreviewed_proposal_block_active(agent) is True
