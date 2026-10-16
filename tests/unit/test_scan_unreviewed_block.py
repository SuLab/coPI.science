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
    """A post the agent's own PI tagged it on (PI-priority) is answered even
    while the agent is blocked — by the unreviewed-proposal block or by the
    active-thread threshold (specs/pi-interaction.md)."""

    def _pi_post(self, engine, channel, content, posted_at=None):
        from src.agent.state import PostRef

        posted_at = posted_at or time.time() - 60
        _post(engine, posted_at, channel, content)
        return PostRef(
            post_id=f"{posted_at:.6f}", channel=channel, sender_agent_id="wu",
            content_snippet=content, posted_at=posted_at, pi_priority=True,
        )

    @staticmethod
    def _reply(target, channel="general"):
        return (
            f'{{"action": "reply", "target_post_id": "{target}", "channel": "{channel}"}}\n'
            "<slack_message>Happy to look at this with you.</slack_message>"
        )

    @pytest.fixture
    def posted(self, setup):
        from unittest.mock import AsyncMock

        engine, _, _ = setup
        engine._post_message = AsyncMock(return_value=True)
        return engine._post_message

    async def test_blocked_agent_replies_to_a_public_pi_priority_post(
        self, setup, posted, monkeypatch,
    ):
        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        pi = self._pi_post(engine, "general", "PI-TAGGED-PUBLIC")
        agent.state.interesting_posts = [pi]
        _block(agent)
        llm.queue = [self._reply(pi.post_id)]

        await engine._phase5_new_post(agent)

        assert len(llm.calls) == 1
        assert "PI-TAGGED-PUBLIC" in llm.prompt()
        posted.assert_awaited_once()
        assert posted.await_args.kwargs["thread_ts"] == pi.post_id
        assert agent.state.interesting_posts == []
        assert pi.post_id in agent.state.active_threads

    async def test_thread_threshold_does_not_stop_a_pi_priority_reply(
        self, setup, posted, monkeypatch,
    ):
        from src.agent.state import ThreadState

        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        for i in range(get_settings().active_thread_threshold):
            agent.state.active_threads[f"t{i}"] = ThreadState(
                thread_id=f"t{i}", channel="general", other_agent_id="wu",
            )
        pi = self._pi_post(engine, "general", "PI-TAGGED-PUBLIC")
        agent.state.interesting_posts = [pi]
        llm.queue = [self._reply(pi.post_id)]

        await engine._phase5_new_post(agent)

        posted.assert_awaited_once()
        assert posted.await_args.kwargs["thread_ts"] == pi.post_id

    async def test_blocked_agent_still_cannot_reply_to_a_post_it_was_not_offered(
        self, setup, posted, monkeypatch,
    ):
        from src.agent.state import PostRef

        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        now = time.time()
        pi = self._pi_post(engine, "general", "PI-TAGGED-PUBLIC", posted_at=now - 60)
        _post(engine, now - 30, "general", "REGULAR-INTERESTING")
        regular_id = f"{now - 30:.6f}"
        agent.state.interesting_posts = [pi, PostRef(
            post_id=regular_id, channel="general", sender_agent_id="wu",
            content_snippet="REGULAR-INTERESTING", posted_at=now - 30,
        )]
        _block(agent)
        llm.queue = [self._reply(regular_id)]

        await engine._phase5_new_post(agent)

        assert "REGULAR-INTERESTING" not in llm.prompts()
        posted.assert_not_awaited()

    @pytest.mark.parametrize("blocked", [True, False])
    async def test_pi_priority_post_on_a_closed_thread_is_dropped(
        self, setup, monkeypatch, blocked,
    ):
        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        pi = self._pi_post(engine, "general", "PI-TAGGED-CLOSED")
        agent.state.interesting_posts = [pi]
        engine._closed_thread_ids.add(pi.post_id)
        if blocked:
            _block(agent)
        llm.queue = ['{"action": "skip"}']

        await engine._phase5_new_post(agent)

        assert agent.state.interesting_posts == []
        assert "PI-TAGGED-CLOSED" not in llm.prompts()

    async def test_pi_priority_post_on_a_thread_reserved_for_others_is_dropped(
        self, setup, monkeypatch,
    ):
        """A human root tagging only @WuBot reserves the thread for wu, so the
        post-LLM reply check would reject every reply: the post must not buy
        an Opus call per turn while it sits in interesting_posts."""
        from src.agent.state import PostRef

        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        engine.message_log.set_bot_name_map({**engine._bot_name_to_id, "wubot": "wu"})
        now = time.time()
        root = f"{now - 60:.6f}"
        engine.message_log.append(LogEntry(
            ts=root, channel="general", sender_agent_id=None, sender_name="Some Human",
            content="@WuBot thoughts on this?", posted_at=now - 60, is_bot=False,
        ))
        assert engine.message_log.get_thread_allowed_agents(root) == {"wu"}
        agent.state.interesting_posts = [PostRef(
            post_id=root, channel="general", sender_agent_id="Andrew Su (PI)",
            content_snippet="@SuBot join this", posted_at=now - 30, pi_priority=True,
        )]
        _block(agent)

        await engine._phase5_new_post(agent)
        await engine._phase5_new_post(agent)

        assert llm.calls == []
        assert agent.state.interesting_posts == []

    async def test_blocked_agent_still_cannot_make_a_regular_new_post(
        self, setup, posted, monkeypatch,
    ):
        engine, agent, llm = setup
        monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
        agent.state.interesting_posts = [self._pi_post(engine, "general", "PI-TAGGED-PUBLIC")]
        _block(agent)
        llm.queue = [
            '{"action": "new_post", "channel": "general", "post_type": "idea"}\n'
            "<slack_message>A brand-new idea.</slack_message>"
        ]

        await engine._phase5_new_post(agent)

        posted.assert_not_awaited()

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
