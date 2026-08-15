"""Tests for simulation engine pure-logic functions."""

import pytest

from src.agent.simulation import (
    SimulationEngine,
    _extract_json,
    _extract_slack_message,
    _strip_llm_preamble,
)

# ---------------------------------------------------------------
# _extract_slack_message
# ---------------------------------------------------------------

class TestExtractSlackMessage:
    def test_extracts_from_tags(self):
        text = """Let me think about this...

<slack_message>
Hi @SuBot — your BioThings Explorer platform is fascinating.
</slack_message>"""
        result = _extract_slack_message(text)
        assert result == "Hi @SuBot — your BioThings Explorer platform is fascinating."

    def test_extracts_multiline_message(self):
        text = """<slack_message>
First paragraph.

Second paragraph with more detail.
</slack_message>"""
        result = _extract_slack_message(text)
        assert "First paragraph." in result
        assert "Second paragraph" in result

    def test_ignores_content_outside_tags(self):
        text = """Internal reasoning about tool results.

<slack_message>
The actual message.
</slack_message>

More internal notes."""
        result = _extract_slack_message(text)
        assert result == "The actual message."
        assert "Internal" not in result
        assert "More internal" not in result

    def test_falls_back_to_preamble_strip_without_tags(self):
        text = "Let me think about this.\n\nHi @SuBot, great to connect!"
        result = _extract_slack_message(text)
        assert result == "Hi @SuBot, great to connect!"

    def test_returns_clean_text_without_tags(self):
        text = "Hi @SuBot, great to connect!"
        assert _extract_slack_message(text) == text

    def test_empty_tags(self):
        text = "<slack_message>\n\n</slack_message>"
        result = _extract_slack_message(text)
        assert result == ""

    def test_ignores_tag_mention_in_reasoning(self):
        # LLM reasoning often mentions the tag name (e.g. quoted in backticks).
        # The extractor must anchor on the real tag pair, not the mention.
        text = (
            "I need to think about what to post.\n\n"
            "The instructions say my output is a single `<slack_message>` "
            "block that gets posted as a reply.\n\n"
            "So I'll write a substantive reply now.\n\n"
            "<slack_message>\n"
            "The actual message content.\n"
            "</slack_message>"
        )
        result = _extract_slack_message(text)
        assert result == "The actual message content."
        assert "block that gets posted" not in result
        assert "substantive reply" not in result


# ---------------------------------------------------------------
# _strip_llm_preamble
# ---------------------------------------------------------------

class TestStripLlmPreamble:
    def test_strips_separator(self):
        text = "Internal reasoning\n---\nActual message"
        assert _strip_llm_preamble(text) == "Actual message"

    def test_strips_multiple_separators(self):
        text = "Note 1\n---\nNote 2\n---\nActual message"
        assert _strip_llm_preamble(text) == "Actual message"

    def test_strips_single_preamble_paragraph(self):
        text = "Let me think about this carefully.\n\nGreat question about cryo-EM!"
        assert _strip_llm_preamble(text) == "Great question about cryo-EM!"

    def test_strips_multi_paragraph_preamble(self):
        text = (
            "That's not relevant. Let me try a different approach.\n\n"
            "Now I have enough context to write a response.\n\n"
            "Hi @LotzBot — this caught my eye."
        )
        assert _strip_llm_preamble(text) == "Hi @LotzBot — this caught my eye."

    def test_preserves_clean_message(self):
        text = "Hi @SuBot, great to connect!"
        assert _strip_llm_preamble(text) == text

    def test_preserves_message_starting_with_emoji(self):
        text = ":newspaper: Paper — We just published on cryo-ET"
        assert _strip_llm_preamble(text) == text

    def test_strips_thinking_preamble(self):
        text = "I should focus on the proteomics angle.\n\nYour ABPP platform is impressive."
        assert _strip_llm_preamble(text) == "Your ABPP platform is impressive."

    def test_strips_tool_result_commentary(self):
        text = (
            "These PubMed searches aren't finding the right papers.\n\n"
            "Hi @WisemanBot — I noticed your lab's recent work on PERK."
        )
        assert _strip_llm_preamble(text) == "Hi @WisemanBot — I noticed your lab's recent work on PERK."

    def test_unfortunately_followed_by_real_message(self):
        text = "Unfortunately the full text isn't available.\n\nYour ABPP platform could help us identify..."
        result = _strip_llm_preamble(text)
        assert result == "Your ABPP platform could help us identify..."


# ---------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------

class TestExtractJson:
    def test_raw_json(self):
        text = '{"selected_post_ids": ["1", "2"]}'
        result = _extract_json(text)
        assert result["selected_post_ids"] == ["1", "2"]

    def test_json_in_code_block(self):
        text = '```json\n{"selected_post_ids": ["1"]}\n```'
        result = _extract_json(text)
        assert result["selected_post_ids"] == ["1"]

    def test_json_with_surrounding_text(self):
        text = 'Here is my response:\n{"action": "reply"}\nDone.'
        result = _extract_json(text)
        assert result["action"] == "reply"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            _extract_json("no json here")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _extract_json("")


# ---------------------------------------------------------------
# _parse_phase5_response (via SimulationEngine instance)
# ---------------------------------------------------------------

class TestParsePhase5Response:
    @pytest.fixture
    def engine(self):
        return SimulationEngine(agents=[], slack_clients={})

    def test_json_plus_slack_message_tags(self, engine):
        response = """```json
{"action": "reply", "target_post_id": "123", "channel": "general", "post_type": "reply", "tagged_agent": null}
```

Some thinking...

<slack_message>
Hi @SuBot — your BioThings work is great!
</slack_message>"""
        data, msg = engine._parse_phase5_response(response)
        assert data["action"] == "reply"
        assert data["target_post_id"] == "123"
        assert msg == "Hi @SuBot — your BioThings work is great!"
        assert "Some thinking" not in msg

    def test_plain_text_without_tags_returns_none(self, engine):
        """Without <slack_message> tags, message should be None (no raw-text fallback)."""
        response = """```json
{"action": "new_post", "channel": "general", "post_type": "paper", "tagged_agent": null, "target_post_id": null}
```

:newspaper: Paper — We just published on cryo-ET of mitochondria."""
        data, msg = engine._parse_phase5_response(response)
        assert data["action"] == "new_post"
        assert msg is None

    def test_uses_last_json_block(self, engine):
        """When LLM revises its decision mid-response, the last JSON block wins."""
        response = """```json
{"action": "new_post", "channel": "general", "post_type": "paper", "tagged_agent": "lotz"}
```

Actually I should skip this turn.

```json
{"action": "skip"}
```"""
        data, msg = engine._parse_phase5_response(response)
        assert data["action"] == "skip"
        assert msg is None

    def test_raw_json_plus_text_no_tags(self, engine):
        """Raw JSON without <slack_message> tags returns None for message."""
        response = '{"action": "new_post", "channel": "general", "post_type": "idea", "tagged_agent": null, "target_post_id": null}\n\n:bulb: Idea — What if we combined...'
        data, msg = engine._parse_phase5_response(response)
        assert data["action"] == "new_post"
        assert msg is None

    def test_malformed_json_returns_none(self, engine):
        data, msg = engine._parse_phase5_response("no json at all, just text")
        assert data is None
        assert msg is None

    def test_json_but_empty_message(self, engine):
        response = '```json\n{"action": "new_post", "channel": "general", "post_type": "idea", "tagged_agent": null, "target_post_id": null}\n```\n'
        data, msg = engine._parse_phase5_response(response)
        assert data is not None
        # Empty or None message
        assert not msg

    def test_ignores_tag_mention_in_reasoning(self, engine):
        # If the LLM mentions `<slack_message>` in its reasoning, extraction
        # must still anchor on the real tag pair, not the mention.
        response = """```json
{"action": "reply", "target_post_id": "123", "channel": "general", "post_type": "reply", "tagged_agent": null}
```

The instructions say my output is a single `<slack_message>` block that gets posted as a reply.

<slack_message>
The actual message.
</slack_message>"""
        data, msg = engine._parse_phase5_response(response)
        assert data["action"] == "reply"
        assert msg == "The actual message."
        assert "block that gets posted" not in msg

    def test_channel_name_preserved(self, engine):
        response = """```json
{"action": "new_post", "channel": "#structural-biology", "post_type": "paper", "tagged_agent": null, "target_post_id": null}
```

<slack_message>
:newspaper: Paper — New finding
</slack_message>"""
        data, msg = engine._parse_phase5_response(response)
        assert data["channel"] == "#structural-biology"

    def test_raw_json_fallback_ignores_a_bare_sidecar(self, engine):
        """A bare <assessment_json> sidecar with no ```json``` action fence at
        all must never be mistaken for the action (Task 8 fix round 1,
        Finding 4) — without the fix this returned
        data={"funnel_stage": "incubation"} and silently discarded the turn."""
        response = (
            '<assessment_json>{"funnel_stage": "incubation"}</assessment_json>'
        )
        data, msg = engine._parse_phase5_response(response)
        assert data is None
        assert msg is None

    def test_raw_json_fallback_still_finds_the_action_past_a_sidecar(self, engine):
        """Stripping the sidecar out of the fallback search must not break the
        fallback's ability to find a real, legitimate raw-JSON action that
        precedes it — the primary ```json``` fence rule is untouched, but the
        raw-JSON fallback must still work when no fence is present."""
        response = (
            '{"action": "new_post", "channel": "general", "post_type": "idea", '
            '"tagged_agent": null, "target_post_id": null}\n\n'
            '<assessment_json>{"funnel_stage": "incubation"}</assessment_json>'
        )
        data, msg = engine._parse_phase5_response(response)
        assert data["action"] == "new_post"


# ---------------------------------------------------------------
# _sync_profiles_from_disk
# ---------------------------------------------------------------

class TestSyncProfilesFromDisk:
    """Per-turn reload of profiles edited from the web app (separate process)."""

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        import src.agent.simulation as sim
        from src.agent.agent import Agent

        (tmp_path / "public").mkdir()
        pub = tmp_path / "public" / "su.md"
        pub.write_text("Focus on aging.")

        # Point the sync method at the temp profiles tree.
        monkeypatch.setattr(sim, "PROFILES_DIR", tmp_path)

        agent = Agent("su", "SuBot", "Andrew Su")
        # Count reload_profiles() calls without losing its real behavior.
        calls = []
        real_reload = agent.reload_profiles
        def counting_reload():
            calls.append(1)
            real_reload()
        agent.reload_profiles = counting_reload

        engine = SimulationEngine(agents=[agent], slack_clients={})
        return engine, agent, pub, calls

    def test_first_observation_records_baseline_without_reload(self, setup):
        engine, agent, _pub, calls = setup
        engine._sync_profiles_from_disk()
        assert calls == []                                  # no reload on first pass
        assert "su" in engine._profile_mtimes              # baseline recorded

    def test_unchanged_files_do_not_reload(self, setup):
        engine, agent, _pub, calls = setup
        engine._sync_profiles_from_disk()  # baseline
        engine._sync_profiles_from_disk()  # nothing changed
        assert calls == []

    def test_external_edit_triggers_reload(self, setup):
        import os
        engine, agent, pub, calls = setup
        engine._sync_profiles_from_disk()  # baseline

        # Simulate the web app rewriting the file. Bump mtime explicitly so the
        # test is robust to sub-second filesystem timestamp resolution.
        pub.write_text("Switch focus to immunology.")
        future = engine._profile_mtimes["su"] + 10
        os.utime(pub, (future, future))

        engine._sync_profiles_from_disk()
        assert calls == [1]                                 # reloaded exactly once
        assert engine._profile_mtimes["su"] == future       # watermark advanced

        # A subsequent pass with no further change must not reload again.
        engine._sync_profiles_from_disk()
        assert calls == [1]

    def test_missing_profile_files_are_tolerated(self, setup, tmp_path):
        engine, agent, pub, calls = setup
        pub.unlink()  # no profile files on disk at all
        engine._sync_profiles_from_disk()  # must not raise
        engine._sync_profiles_from_disk()
        assert calls == []


# ---------------------------------------------------------------
# _rewind_cursors_for_private_channels — rewind tightly, never into
# settled sibling channels (the overshoot bug).
# ---------------------------------------------------------------

class TestRewindCursorsForPrivateChannels:
    def _engine(self):
        from src.agent.agent import Agent
        su = Agent("su", "SuBot", "Andrew Su")
        lairson = Agent("lairson", "LairsonBot", "Brian Lairson")
        engine = SimulationEngine(agents=[su, lairson], slack_clients={})
        return engine, su, lairson

    def _add_channel(self, engine, name, cid, members, msgs):
        """msgs: list of (posted_at, sender_agent_id)."""
        from src.agent.message_log import LogEntry
        engine._channel_id_map[name] = cid
        engine._private_channel_members[cid] = set(members)
        for posted_at, sender in msgs:
            engine.message_log.append(LogEntry(
                ts=f"{posted_at:.6f}", channel=name, sender_agent_id=sender,
                sender_name=sender, content="x", thread_ts=None,
                posted_at=posted_at, is_bot=True,
            ))

    def test_does_not_rewind_into_settled_sibling_channel(self):
        import time
        engine, su, lairson = self._engine()
        now = time.time()
        # Fresh channel: su posted the handover 1h ago, lairson hasn't replied.
        self._add_channel(engine, "june", "CJUN", ["su", "lairson"],
                           [(now - 3600, "su")])
        # Stale sibling: a finished refinement from ~60 days ago.
        old = now - 60 * 86400
        self._add_channel(engine, "april", "CAPR", ["su", "lairson"],
                           [(old, "su"), (old + 100, "lairson"), (old + 200, "su")])
        su.state.last_seen_cursor = now
        lairson.state.last_seen_cursor = now

        engine._rewind_cursors_for_private_channels()

        # lairson is rewound only into the FRESH channel (just before the
        # handover), never back to April.
        assert abs(lairson.state.last_seen_cursor - (now - 3600 - 0.001)) < 0.01
        assert lairson.state.last_seen_cursor > old + 1000
        # su authored the only fresh message and April is settled → no rewind.
        assert su.state.last_seen_cursor == now

    def test_rewinds_to_unacted_reply_for_ongoing_refinement(self):
        import time
        engine, su, lairson = self._engine()
        now = time.time()
        # su handover, then lairson's refinement reply 30m ago (unacted by su).
        self._add_channel(engine, "june", "CJUN", ["su", "lairson"],
                           [(now - 3600, "su"), (now - 1800, "lairson")])
        su.state.last_seen_cursor = now
        lairson.state.last_seen_cursor = now

        engine._rewind_cursors_for_private_channels()

        # su rewinds just before lairson's reply (not back to the handover).
        assert abs(su.state.last_seen_cursor - (now - 1800 - 0.001)) < 0.01
        # lairson posted last → caught up → not rewound.
        assert lairson.state.last_seen_cursor == now

    def test_cursor_only_moves_backward(self):
        import time
        engine, su, lairson = self._engine()
        now = time.time()
        self._add_channel(engine, "june", "CJUN", ["su", "lairson"],
                           [(now - 3600, "su")])
        # lairson's cursor is already far in the past — rewind must not drag it
        # forward to the (more recent) target.
        lairson.state.last_seen_cursor = now - 10 * 86400
        su.state.last_seen_cursor = now

        engine._rewind_cursors_for_private_channels()
        assert lairson.state.last_seen_cursor == now - 10 * 86400

    def test_noop_when_channel_has_no_messages_yet(self):
        import time
        engine, su, lairson = self._engine()
        now = time.time()
        engine._channel_id_map["june"] = "CJUN"
        engine._private_channel_members["CJUN"] = {"su", "lairson"}
        su.state.last_seen_cursor = now
        lairson.state.last_seen_cursor = now
        engine._rewind_cursors_for_private_channels()
        assert su.state.last_seen_cursor == now
        assert lairson.state.last_seen_cursor == now


# ---------------------------------------------------------------
# mint_ts — monotonic, unique, ts-shaped ids (DB-primary store)
# ---------------------------------------------------------------

class TestMintTs:
    def test_monotonic_and_unique_under_tight_loop(self):
        engine = SimulationEngine(agents=[], slack_clients={})
        ids = [engine.mint_ts() for _ in range(1000)]
        floats = [float(x) for x in ids]
        # Strictly increasing (so posted_at=float(ts) ordering is preserved).
        # This is the regression guard for the float-precision bug: at the
        # current epoch magnitude the old f"{time.time():.6f}" scheme produced
        # ids that were equal (or non-increasing) once round-tripped to float.
        assert all(b > a for a, b in zip(floats, floats[1:], strict=False))
        # All unique
        assert len(set(ids)) == len(ids)

    def test_ids_are_ts_shaped_with_six_decimal_microseconds(self):
        engine = SimulationEngine(agents=[], slack_clients={})
        ts = engine.mint_ts()
        secs, _, micros = ts.partition(".")
        assert secs.isdigit()
        assert len(micros) == 6 and micros.isdigit()

    def test_seeded_high_water_mark_sorts_after_history(self):
        import time

        engine = SimulationEngine(agents=[], slack_clients={})
        # Simulate a rebuild that saw history slightly ahead of the wall clock.
        future = time.time() + 3600
        engine._ts_minter.seed_floor(future)
        assert float(engine.mint_ts()) > future


# ---------------------------------------------------------------
# _flush_persisted — a failed flush must NOT drop conversation content
# (H1). The DB is the primary store, so a dropped batch is unrecoverable.
# ---------------------------------------------------------------

class TestFlushPersistedFailure:
    def _entry(self, ts, content):
        from src.agent.message_log import LogEntry

        return LogEntry(
            ts=ts,
            channel="general",
            sender_agent_id="su",
            sender_name="subot",
            content=content,
            posted_at=float(ts),
        )

    @pytest.mark.asyncio
    async def test_failed_flush_requeues_batch(self):
        import uuid

        def failing_factory():
            raise RuntimeError("transient DB error")

        engine = SimulationEngine(
            agents=[],
            slack_clients={},
            session_factory=failing_factory,
            simulation_run_id=uuid.uuid4(),
        )
        engine._pending_persist = [
            self._entry("100.000001", "first"),
            self._entry("100.000002", "second"),
        ]

        await engine._flush_persisted()

        # The batch must survive for the next attempt, not vanish.
        assert len(engine._pending_persist) == 2
        assert [e.ts for e in engine._pending_persist] == ["100.000001", "100.000002"]

    @pytest.mark.asyncio
    async def test_requeued_batch_preserves_order_ahead_of_new_entries(self):
        import uuid

        def failing_factory():
            raise RuntimeError("transient DB error")

        engine = SimulationEngine(
            agents=[],
            slack_clients={},
            session_factory=failing_factory,
            simulation_run_id=uuid.uuid4(),
        )
        engine._pending_persist = [
            self._entry("100.000001", "old-1"),
            self._entry("100.000002", "old-2"),
        ]
        await engine._flush_persisted()
        # A newer entry arrives after the failed flush re-queued the old batch;
        # the re-queued batch must remain chronologically ahead of it.
        engine._pending_persist.append(self._entry("100.000003", "new"))

        assert [e.ts for e in engine._pending_persist] == [
            "100.000001",
            "100.000002",
            "100.000003",
        ]

    @pytest.mark.asyncio
    async def test_no_db_clears_buffer(self):
        # Without a session_factory the buffer is intentionally dropped so it
        # can't grow unbounded — the re-queue path must not change that.
        engine = SimulationEngine(agents=[], slack_clients={})
        engine._pending_persist = [self._entry("100.000001", "x")]
        await engine._flush_persisted()
        assert engine._pending_persist == []


# ---------------------------------------------------------------
# _flush_llm_logs — same requirement as _flush_persisted above (H1): a failed
# flush must not drop the buffered LLM call logs. Unlike the three writers
# below, this one already has a natural retry buffer (_llm_log_buffer, drained
# on a timer from the main loop each turn), so the fix mirrors
# _flush_persisted's re-queue exactly, just against a different buffer.
# ---------------------------------------------------------------

class TestFlushLlmLogsFailure:
    @pytest.mark.asyncio
    async def test_failed_flush_requeues_batch(self):
        import uuid

        def failing_factory():
            raise RuntimeError("transient DB error")

        engine = SimulationEngine(
            agents=[], slack_clients={},
            session_factory=failing_factory, simulation_run_id=uuid.uuid4(),
        )
        engine._llm_log_buffer = [
            {"agent_id": "su", "phase": "phase4"},
            {"agent_id": "wu", "phase": "phase5"},
        ]

        await engine._flush_llm_logs()

        # The batch must survive for the next attempt, not vanish.
        assert len(engine._llm_log_buffer) == 2

    @pytest.mark.asyncio
    async def test_requeued_batch_preserves_order_ahead_of_new_entries(self):
        import uuid

        def failing_factory():
            raise RuntimeError("transient DB error")

        engine = SimulationEngine(
            agents=[], slack_clients={},
            session_factory=failing_factory, simulation_run_id=uuid.uuid4(),
        )
        engine._llm_log_buffer = [
            {"agent_id": "su", "phase": "old-1"},
            {"agent_id": "su", "phase": "old-2"},
        ]
        await engine._flush_llm_logs()
        # A newer entry arrives after the failed flush re-queued the old batch;
        # the re-queued batch must remain chronologically ahead of it.
        engine._llm_log_buffer.append({"agent_id": "su", "phase": "new"})

        assert [e["phase"] for e in engine._llm_log_buffer] == [
            "old-1", "old-2", "new",
        ]


# ---------------------------------------------------------------
# _persist_assessment — a fully-built OpportunityAssessment row (Task 2 fix
# round 1, Finding 1). Structurally it is the same shape as the message-log/
# LLM-log buffers above: every field is computed synchronously before the
# write, and nothing else in-process reads the row back the way MessageLog
# does — so, unlike _record_assessment_drop/_close_thread below, a failure
# here IS requeued, draining through _flush_pending_assessments on the same
# per-turn cadence as _flush_persisted/_flush_llm_logs (see _run_main_loop
# and stop()). This table is the actual product of the screening pipeline,
# so the first-attempt failure still logs LOUD (ERROR + traceback) even
# though it is now recoverable — visibility and durability are both kept.
# ---------------------------------------------------------------

class TestPersistAssessmentRequeue:
    @staticmethod
    def _failing_engine():
        import uuid

        def failing_factory():
            raise RuntimeError("pool checkout timed out")

        return SimulationEngine(
            agents=[], slack_clients={},
            session_factory=failing_factory, simulation_run_id=uuid.uuid4(),
        )

    @pytest.mark.asyncio
    async def test_first_failure_queues_the_row_instead_of_dropping_it(self, caplog):
        engine = self._failing_engine()

        with caplog.at_level("ERROR"):
            await engine._persist_assessment("blackbird", "general", {"scores": {}})

        # The row must survive as a queued retry, not vanish.
        assert len(engine._pending_assessments) == 1
        assert engine._pending_assessments[0]["agent_id"] == "blackbird"
        # Still loud on the first failure — a full traceback, and explicit
        # that this is queued (not "LOST": there IS a retry path now).
        assert "queued for retry" in caplog.text
        assert "LOST" not in caplog.text
        assert any(r.exc_info for r in caplog.records)

    @pytest.mark.asyncio
    async def test_flush_requeues_a_batch_that_fails_again(self):
        engine = self._failing_engine()
        engine._pending_assessments = [
            {"simulation_run_id": engine.simulation_run_id, "agent_id": "su",
             "channel_name": "general"},
            {"simulation_run_id": engine.simulation_run_id, "agent_id": "wu",
             "channel_name": "general"},
        ]

        await engine._flush_pending_assessments()

        # The batch must survive for the next attempt, not vanish.
        assert len(engine._pending_assessments) == 2

    @pytest.mark.asyncio
    async def test_flush_requeued_batch_preserves_order_ahead_of_new_entries(self):
        engine = self._failing_engine()
        engine._pending_assessments = [
            {"simulation_run_id": engine.simulation_run_id, "agent_id": "old-1"},
            {"simulation_run_id": engine.simulation_run_id, "agent_id": "old-2"},
        ]
        await engine._flush_pending_assessments()
        # A newer failure queues after the failed flush re-queued the old
        # batch; the re-queued batch must remain ahead of it.
        engine._pending_assessments.append(
            {"simulation_run_id": engine.simulation_run_id, "agent_id": "new"}
        )

        assert [r["agent_id"] for r in engine._pending_assessments] == [
            "old-1", "old-2", "new",
        ]

    @pytest.mark.asyncio
    async def test_no_db_clears_buffer(self):
        # Without a session_factory the buffer is intentionally dropped so it
        # can't grow unbounded — matches _flush_persisted's own no-DB case.
        engine = SimulationEngine(agents=[], slack_clients={})
        engine._pending_assessments = [{"agent_id": "su"}]
        await engine._flush_pending_assessments()
        assert engine._pending_assessments == []


# ---------------------------------------------------------------
# _record_assessment_drop / _close_thread — neither has a natural retry
# buffer the way _flush_persisted/_flush_llm_logs/_flush_pending_assessments
# do: each is a single, already-final write triggered once per event rather
# than drained from an accumulating list on a timer, so a DB failure here
# can't be requeued without inventing a queue purpose-built for just that
# write. Instead the failure is made LOUD — ERROR level plus a full
# traceback — so a pool-checkout timeout reads as unmistakable data loss
# rather than routine background noise indistinguishable from any other
# warning in the log.
# ---------------------------------------------------------------

class TestUnretryableWriteFailuresAreLoud:
    @pytest.mark.asyncio
    async def test_record_assessment_drop_failure_logs_a_traceback(self, caplog):
        import uuid

        def failing_factory():
            raise RuntimeError("pool checkout timed out")

        engine = SimulationEngine(
            agents=[], slack_clients={},
            session_factory=failing_factory, simulation_run_id=uuid.uuid4(),
        )
        with caplog.at_level("ERROR"):
            await engine._record_assessment_drop("blackbird", "specialist_floor")

        assert "LOST" in caplog.text
        assert any(r.exc_info for r in caplog.records)

    @pytest.mark.asyncio
    async def test_close_thread_db_failure_logs_a_traceback(self, caplog, monkeypatch):
        import uuid

        from src.agent.agent import Agent
        from src.agent.state import ThreadState

        def failing_factory():
            raise RuntimeError("pool checkout timed out")

        agent = Agent("blackbird", "BlackbirdBot", "Blackbird")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            message_count=3, has_pending_reply=False,
        )
        agent.state.active_threads["t1"] = thread
        engine = SimulationEngine(
            agents=[agent], slack_clients={},
            session_factory=failing_factory, simulation_run_id=uuid.uuid4(),
        )

        async def _noop_memory_update(*a, **kw):
            return None

        monkeypatch.setattr(engine, "_update_agent_memory", _noop_memory_update)

        with caplog.at_level("ERROR"):
            await engine._close_thread(agent, thread, "no_proposal")

        assert "LOST" in caplog.text
        assert any(r.exc_info for r in caplog.records)


# ---------------------------------------------------------------
# Graceful shutdown (R2). A hard kill loses the in-flight turn's messages
# because the DB — not Slack — is now the durable store, so the SIGTERM path
# must (a) cut short the idle backoff and (b) leave the flush awaitable on the
# main coroutine rather than in a cancellable fire-and-forget task.
# ---------------------------------------------------------------

class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_request_stop_ends_the_loop_and_does_no_io(self):
        engine = SimulationEngine(agents=[], slack_clients={})
        engine._running = True
        engine.request_stop()
        assert engine._running is False
        assert engine._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_sleep_returns_early_once_stop_is_requested(self):
        import asyncio
        import time

        engine = SimulationEngine(agents=[], slack_clients={})

        async def stop_soon():
            await asyncio.sleep(0.01)
            engine.request_stop()

        started = time.monotonic()
        # 30s is the longest idle backoff the main loop uses; without the
        # stop-event wakeup this would outlast the container's stop grace period.
        await asyncio.gather(engine._sleep(30), stop_soon())
        assert time.monotonic() - started < 1.0

    @pytest.mark.asyncio
    async def test_sleep_is_a_no_op_after_stop(self):
        import time

        engine = SimulationEngine(agents=[], slack_clients={})
        engine.request_stop()
        started = time.monotonic()
        await engine._sleep(30)
        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    async def test_stop_flushes_the_pending_buffer(self):
        # stop() must drain the buffer, not just flip the flag — it is the last
        # chance to persist the in-flight turn.
        engine = SimulationEngine(agents=[], slack_clients={})
        flushed = []

        async def fake_flush(force_stats=False):
            flushed.append(force_stats)
            engine._pending_persist.clear()

        engine._flush_persisted = fake_flush
        engine._pending_persist = [self._entry("100.000001", "in-flight")]

        await engine.stop()

        assert flushed == [True]  # forced final stats refresh
        assert engine._pending_persist == []
        assert engine._running is False

    def _entry(self, ts, content):
        from src.agent.message_log import LogEntry

        return LogEntry(
            ts=ts, channel="general", sender_agent_id="su",
            sender_name="subot", content=content, posted_at=float(ts),
        )


# ---------------------------------------------------------------
# Slack thread-parent translation. Slack threads on the *root's Slack ts*; the
# canonical thread_ts is only the same thing when the root was born on Slack. A
# thread started with Slack off has a minted root id, which Slack has never seen
# — mirroring a reply into it detaches the message or errors.
# ---------------------------------------------------------------

class TestSlackParentTranslation:
    def _engine_with_client(self):
        from src.agent.agent import Agent
        from tests.fakes import FakeSlackClient

        agent = Agent("su", "SuBot", "Andrew Su")
        client = FakeSlackClient(agent_id="su")
        return SimulationEngine(agents=[agent], slack_clients={"su": client}), client

    def _root(self, ts, *, slack_ts=None):
        from src.agent.message_log import LogEntry

        return LogEntry(
            ts=ts, channel="general", sender_agent_id="su", sender_name="SuBot",
            content="root", posted_at=float(ts), is_bot=True, slack_ts=slack_ts,
        )

    def test_resolves_a_slack_backed_root_to_its_slack_ts(self):
        engine, _ = self._engine_with_client()
        # DB-origin root later mirrored: canonical id != Slack ts.
        engine.message_log.append(self._root("1700000000.000000", slack_ts="1700009999.111111"))
        assert engine._slack_parent_ts("1700000000.000000") == "1700009999.111111"

    def test_returns_none_for_a_db_origin_root(self):
        engine, _ = self._engine_with_client()
        engine.message_log.append(self._root("1700000000.000000"))  # never mirrored
        assert engine._slack_parent_ts("1700000000.000000") is None

    def test_falls_back_to_the_canonical_id_when_the_root_is_unknown(self):
        # Root windowed out by the B2 rebuild bound: preserve pure-Slack-on
        # behaviour, where the canonical id *is* the Slack ts.
        engine, _ = self._engine_with_client()
        assert engine._slack_parent_ts("1700000000.000000") == "1700000000.000000"

    def test_top_level_post_has_no_parent(self):
        engine, _ = self._engine_with_client()
        assert engine._slack_parent_ts(None) is None

    @pytest.mark.asyncio
    async def test_reply_is_mirrored_against_the_roots_slack_ts(self):
        engine, client = self._engine_with_client()
        engine.message_log.append(self._root("1700000000.000000", slack_ts="1700009999.111111"))

        await engine._post_message("su", "general", "a reply", thread_ts="1700000000.000000")

        assert len(client.posted) == 1
        # Slack receives the root's Slack ts, never the minted canonical id.
        assert client.posted[0]["thread_ts"] == "1700009999.111111"

    @pytest.mark.asyncio
    async def test_reply_into_a_slackless_thread_is_not_mirrored(self):
        # The mid-life-toggle case: thread started Slack-off, Slack now on.
        engine, client = self._engine_with_client()
        engine.message_log.append(self._root("1700000000.000000"))

        await engine._post_message("su", "general", "a reply", thread_ts="1700000000.000000")

        assert client.posted == []  # no bogus thread_ts sent to Slack
        # ...but the message is still recorded in the DB-primary log.
        replies = [e for e in engine.message_log._entries if e.thread_ts == "1700000000.000000"]
        assert len(replies) == 1
        assert replies[0].content == "a reply"
        assert replies[0].slack_ts is None
        assert replies[0].slack_thread_ts is None

    @pytest.mark.asyncio
    async def test_mirrored_reply_records_the_slack_parent_mapping(self):
        engine, _ = self._engine_with_client()
        engine.message_log.append(self._root("1700000000.000000", slack_ts="1700009999.111111"))

        await engine._post_message("su", "general", "a reply", thread_ts="1700000000.000000")

        reply = [e for e in engine.message_log._entries if e.thread_ts == "1700000000.000000"][0]
        assert reply.slack_thread_ts == "1700009999.111111"
        assert reply.thread_ts == "1700000000.000000"  # canonical id unchanged


# ---------------------------------------------------------------
# _post_message must never let the <assessment_json> verdict sidecar reach
# Slack — it is for Blackbird staff and the DB, never for the channel the
# assessed scientist reads. Placed here (rather than in
# tests/unit/test_assessment_sidecar.py) because it needs the
# FakeSlackClient-backed engine this class's harness already provides:
# these tests drive the real _post_message text-cleaning path end-to-end and
# assert on what a Slack client would actually receive
# (`client.posted[i]["text"]`), instead of testing the strip regexes in
# isolation. test_assessment_sidecar.py's tests never call _post_message at
# all, so before this class existed the anti-leak property had zero coverage
# (Task 8 fix round 1, Finding 2).
# ---------------------------------------------------------------

class TestPostMessageStripsAssessmentSidecar:
    def _engine_with_client(self):
        from src.agent.agent import Agent
        from tests.fakes import FakeSlackClient

        agent = Agent("su", "SuBot", "Andrew Su")
        client = FakeSlackClient(agent_id="su")
        return SimulationEngine(agents=[agent], slack_clients={"su": client}), client

    # Distinctive fragments that only ever appear inside the verdict JSON —
    # deliberately not "incubation" alone, since that word can legitimately
    # appear in the recommendation prose of a real Slack body.
    _VERDICT_MARKERS = (
        "assessment_json",
        "funnel_stage",
        '"differentiation": 4',
        "No external validation yet",
    )

    def _assert_no_verdict_leaked(self, posted_text):
        lowered = posted_text.lower()
        for marker in self._VERDICT_MARKERS:
            assert marker.lower() not in lowered, f"leaked {marker!r} into: {posted_text!r}"

    @pytest.mark.asyncio
    async def test_well_formed_sidecar_nested_in_slack_message_never_reaches_slack(self):
        # A model that mistakenly puts the sidecar *inside* <slack_message>
        # instead of after it.
        engine, client = self._engine_with_client()
        text = (
            "<slack_message>\n"
            ":mag: *Opportunity Assessment — Wang Lab (JHU)*\n"
            "Recommendation: proceed to diligence. [Speculative]\n"
            "<assessment_json>\n"
            '{"funnel_stage": "incubation", '
            '"scores": {"differentiation": 4}, '
            '"red_flags": ["No external validation yet"]}\n'
            "</assessment_json>\n"
            "</slack_message>"
        )

        await engine._post_message("su", "general", text)

        assert len(client.posted) == 1
        posted_text = client.posted[0]["text"]
        self._assert_no_verdict_leaked(posted_text)
        # The legitimate body must survive, not just the verdict be gone.
        assert "Opportunity Assessment" in posted_text
        assert "Recommendation: proceed to diligence" in posted_text

    @pytest.mark.asyncio
    async def test_unclosed_sidecar_is_dropped_to_end_of_text_not_leaked(self):
        # Simulates a Phase 5 response truncated mid-sidecar (Finding 1): the
        # closing </assessment_json> never arrives because max_tokens was hit.
        engine, client = self._engine_with_client()
        text = (
            "<slack_message>Legit body.\n"
            '<assessment_json>{"funnel_stage": "incubation", '
            '"red_flags": ["No external validation yet"]}'
        )

        await engine._post_message("su", "general", text)

        posted_text = client.posted[0]["text"]
        self._assert_no_verdict_leaked(posted_text)
        # Losing trailing prose after an unclosed tag is the accepted
        # trade-off; the text *before* the orphaned tag must be untouched.
        assert posted_text == "Legit body."

    @pytest.mark.asyncio
    async def test_uppercase_and_spaced_tag_variants_are_stripped(self):
        # Finding 3: the tag match must be case-insensitive and tolerant of
        # stray whitespace inside the delimiters.
        engine, client = self._engine_with_client()
        text = (
            "<slack_message>\n"
            "Legit body.\n"
            "<ASSESSMENT_JSON >\n"
            '{"funnel_stage": "incubation", '
            '"red_flags": ["No external validation yet"]}\n'
            "</ASSESSMENT_JSON >\n"
            "</slack_message>"
        )

        await engine._post_message("su", "general", text)

        posted_text = client.posted[0]["text"]
        self._assert_no_verdict_leaked(posted_text)
        assert posted_text == "Legit body."

    @pytest.mark.asyncio
    async def test_unclosed_sidecar_with_no_body_suppresses_the_post(self):
        # Fix round 2 finding: an unclosed sidecar with NO legitimate text
        # before it strips to "". Before this fix, _post_message still posted
        # the empty string to Slack and wrote a phantom LogEntry
        # (content="", slack_ts=None) for a message that was never actually
        # published — a DB row with no corresponding Slack message, and the
        # caller's turn silently consumed for nothing. Must suppress
        # entirely: no Slack call, no log entry.
        engine, client = self._engine_with_client()
        text = '<assessment_json>{"funnel_stage": "incubation", "red_flags": ["danger"]'

        await engine._post_message("su", "general", text)

        assert client.posted == []
        assert engine.message_log._entries == []

    @pytest.mark.asyncio
    async def test_well_formed_sidecar_as_the_entire_message_suppresses_the_post(self):
        # The reachable shape behind the finding: a model nests the sidecar
        # as the *entire* <slack_message> body, with nothing else in it —
        # well-formed this time, but still nothing left to post once the
        # verdict is stripped out.
        engine, client = self._engine_with_client()
        text = (
            "<slack_message>"
            "<assessment_json>"
            '{"funnel_stage": "incubation", "red_flags": ["danger"]}'
            "</assessment_json>"
            "</slack_message>"
        )

        await engine._post_message("su", "general", text)

        assert client.posted == []
        assert engine.message_log._entries == []


# ---------------------------------------------------------------
# A suppressed _post_message (return value falsy — text stripped to nothing
# by the sidecar/tag cleanup inside it) must not count a turn or drain state
# at any call site (Finding A2). The phase-5 "New top-level post" branch
# already got this right; these tests cover the phase-4 reply call site and
# the two phase-5 reply-action branches, which did not check the return
# value at all before the fix.
# ---------------------------------------------------------------

# A response whose <slack_message> body is entirely an unclosed
# <assessment_json> tag: non-empty at every check that runs before
# _post_message (so it is never mistaken for the empty-response case those
# checks already guard), but strips to nothing once _post_message removes
# the sidecar/tag markup — the exact suppression case Finding A2 is about.
_SUPPRESSING_SLACK_MESSAGE = (
    '<slack_message><assessment_json>{"funnel_stage": "incubation"</slack_message>'
)


class TestPhase4ReplySuppression:
    def _engine_with_thread(self):
        from src.agent.agent import Agent
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        agent = Agent("blackbird", "BlackbirdBot", "Blackbird")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            message_count=0, has_pending_reply=True,
        )
        agent.state.active_threads["t1"] = thread
        client = FakeSlackClient(agent_id="blackbird")
        engine = SimulationEngine(agents=[agent], slack_clients={"blackbird": client})
        return engine, agent, thread, client

    @pytest.mark.asyncio
    async def test_suppressed_reply_is_not_counted_or_drained(self, monkeypatch, caplog):
        caplog.set_level("INFO")
        engine, agent, thread, client = self._engine_with_thread()

        async def _fake_generate_with_tools(**kwargs):
            return _SUPPRESSING_SLACK_MESSAGE

        monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )

        await engine._reply_to_thread(agent, thread)

        assert client.posted == []
        assert agent.message_count == 0
        # Nothing actually went out, so the reply must still be pending —
        # clearing it here would silently drop the thread instead of retrying.
        assert thread.has_pending_reply is True
        assert thread.empty_response_count == 0
        assert "suppressed" in caplog.text
        assert "not counted" in caplog.text

    @pytest.mark.asyncio
    async def test_non_suppressed_reply_still_counts_and_drains(self, monkeypatch):
        """The non-suppressed path must be unchanged by the fix."""
        engine, agent, thread, client = self._engine_with_thread()

        async def _fake_generate_with_tools(**kwargs):
            return "<slack_message>A normal reply.</slack_message>"

        monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )

        await engine._reply_to_thread(agent, thread)

        assert len(client.posted) == 1
        assert client.posted[0]["text"] == "A normal reply."
        assert agent.message_count == 1
        assert thread.has_pending_reply is False


# ---------------------------------------------------------------
# The pending/reactive-priority trigger loop closed 2026-08-12
# (PI-interaction removal cycle). The surviving `is_bot=False` producer
# (`reopen_proposal` -> `src/services/pi_inbox.py::record_pi_message`) writes
# a human-authored row that the DB-inbound poller ingests into the shared
# MessageLog; before this fix, `MessageLog.has_new_reply_from_other` (via
# `_owes_reply` and the reply lane's ungated call, `_pending_reply_pairs`
# since Task 11) would have treated that row as "a new reply from the other
# participant" — setting `has_pending_reply`, granting reactive priority, and
# (via `_reply_to_thread`'s message-count recompute) shifting the thread's
# ordinal.
# ---------------------------------------------------------------

class TestHumanRepliesAreInertToPhase4:
    def _engine_with_thread(self):
        from src.agent.agent import Agent
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        agent = Agent("blackbird", "BlackbirdBot", "Blackbird")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            message_count=3, has_pending_reply=False,
        )
        agent.state.active_threads["t1"] = thread
        client = FakeSlackClient(agent_id="blackbird")
        engine = SimulationEngine(agents=[agent], slack_clients={"blackbird": client})
        return engine, agent, thread, client

    @staticmethod
    def _human_entry(ts="2", content="guidance"):
        from src.agent.message_log import LogEntry
        return LogEntry(
            ts=ts, channel="general", sender_agent_id=None,
            sender_name="Dr Wang (PI)", content=content, thread_ts="t1",
            posted_at=float(ts), is_bot=False,
        )

    @staticmethod
    def _bot_entry(ts="2", content="real reply"):
        from src.agent.message_log import LogEntry
        return LogEntry(
            ts=ts, channel="general", sender_agent_id="wang", sender_name="WangBot",
            content=content, thread_ts="t1", posted_at=float(ts), is_bot=True,
        )

    def test_human_reply_does_not_grant_reactive_priority(self):
        engine, agent, _thread, _client = self._engine_with_thread()
        engine.message_log.append(self._human_entry())

        assert engine._owes_reply(agent) is False

    def test_control_bot_reply_does_grant_reactive_priority(self):
        """Positive control: the same shape of entry, bot-authored, DOES owe
        a reply — so the test above is provably about is_bot."""
        engine, agent, _thread, _client = self._engine_with_thread()
        engine.message_log.append(self._bot_entry())

        assert engine._owes_reply(agent) is True

    @pytest.mark.asyncio
    async def test_human_reply_does_not_trigger_phase4_or_shift_the_ordinal(self, monkeypatch):
        engine, agent, thread, _client = self._engine_with_thread()
        engine.message_log.append(self._human_entry())

        called = {"reply": False}

        async def _fake_reply_to_thread(a, t):
            called["reply"] = True

        monkeypatch.setattr(engine, "_reply_to_thread", _fake_reply_to_thread)

        pairs = engine._pending_reply_pairs()

        assert pairs == [], "a human-only entry must not select the thread for reply"
        assert called["reply"] is False
        assert thread.has_pending_reply is False
        assert thread.message_count == 3, (
            "the ordinal must not shift merely from a human entry landing in the thread"
        )

    @pytest.mark.asyncio
    async def test_control_bot_reply_does_trigger_phase4(self, monkeypatch):
        """Positive control for the test above: the same shape of entry,
        bot-authored, DOES select the thread for reply."""
        engine, agent, thread, _client = self._engine_with_thread()
        engine.message_log.append(self._bot_entry())

        called = {"reply": False}

        async def _fake_reply_to_thread(a, t):
            called["reply"] = True

        monkeypatch.setattr(engine, "_reply_to_thread", _fake_reply_to_thread)

        pairs = engine._pending_reply_pairs()
        assert [(a.agent_id, t.thread_id) for a, t in pairs] == [("blackbird", "t1")]

        for a, t in pairs:
            await engine._service_reply(a, t)

        assert called["reply"] is True


# ---------------------------------------------------------------
# Option A relocation: the hub's :mag: Opportunity Assessment is extracted
# from, and stripped out of, its own Phase-4 CONCLUDE reply — see
# SimulationEngine._reply_to_thread / _capture_hub_assessment. The DB-backed
# row-persistence assertions live in
# tests/integration/test_opportunity_assessment_persistence.py; these are the
# fast, no-database pins: the sidecar never reaches Slack, a role check gates
# the whole mechanism to scout_hub, and a persistence failure never crashes
# the reply that already posted.
# ---------------------------------------------------------------

class TestHubAssessmentRelocation:
    def _engine_with_hub_thread(self):
        from src.agent.agent import Agent
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            message_count=11, has_pending_reply=True,
        )
        hub.state.active_threads["t1"] = thread
        client = FakeSlackClient(agent_id="blackbird")
        engine = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
        return engine, hub, thread, client

    @pytest.mark.asyncio
    async def test_sidecar_never_reaches_slack_in_a_concluding_hub_reply(self, monkeypatch):
        """Mission pin: the sidecar must NEVER appear in posted text."""
        engine, hub, thread, client = self._engine_with_hub_thread()

        raw_response = (
            "<slack_message>\n"
            ":mag: Closing note — thanks for the detail.\n"
            "</slack_message>\n\n"
            '<assessment_json>\n'
            '{"subject_agent_id": "wang", "recommendation": "pass", '
            '"scores": {"differentiation": 2}}\n'
            '</assessment_json>'
        )

        async def _fake_generate_with_tools(**kwargs):
            return raw_response

        monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )

        await engine._reply_to_thread(hub, thread)

        assert len(client.posted) == 1
        posted_text = client.posted[0]["text"]
        assert posted_text == ":mag: Closing note — thanks for the detail."
        for leaked in ("assessment_json", "subject_agent_id", "differentiation"):
            assert leaked not in posted_text, f"sidecar leaked into Slack: {leaked!r}"

    @pytest.mark.asyncio
    async def test_a_persistence_failure_is_logged_and_never_crashes_the_reply(
        self, monkeypatch, caplog,
    ):
        """Mission pin (d), the crash-safety half: whatever goes wrong
        downstream of extraction must never propagate out of
        `_reply_to_thread` — the reply already posted and must stay posted."""
        engine, hub, thread, client = self._engine_with_hub_thread()

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(engine, "_persist_assessment", _boom)

        raw_response = (
            "<slack_message>Closing note.</slack_message>\n\n"
            '<assessment_json>{"subject_agent_id": "wang", "recommendation": "pass"}'
            "</assessment_json>"
        )

        async def _fake_generate_with_tools(**kwargs):
            return raw_response

        monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )

        with caplog.at_level("ERROR"):
            await engine._reply_to_thread(hub, thread)

        assert len(client.posted) == 1  # the reply still posted
        assert hub.message_count == 1
        assert "Failed to extract/persist the assessment sidecar" in caplog.text

    @pytest.mark.asyncio
    async def test_pi_lab_replies_never_attempt_assessment_capture(self, monkeypatch):
        """A pi_lab reply must never even try to extract a sidecar — the
        Option A call site is gated on `agent.role == "scout_hub"`."""
        from src.agent.agent import Agent
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        lab = Agent("gill", "GillBot", "Gill", role="pi_lab")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="blackbird",
            message_count=11, has_pending_reply=True,
        )
        lab.state.active_threads["t1"] = thread
        client = FakeSlackClient(agent_id="gill")
        engine = SimulationEngine(agents=[lab], slack_clients={"gill": client})

        called = {"capture": False}

        async def _spy(*args, **kwargs):
            called["capture"] = True

        monkeypatch.setattr(engine, "_capture_hub_assessment", _spy)
        monkeypatch.setattr(lab, "build_phase4_prompt", lambda **kw: ("sys", []))

        async def _fake_generate_with_tools(**kwargs):
            return "<slack_message>A normal reply.</slack_message>"

        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )

        await engine._reply_to_thread(lab, thread)

        assert called["capture"] is False
        assert len(client.posted) == 1


# ---------------------------------------------------------------
# Ordinal regression pin (fix round T6, round 2). `_reply_to_thread` passed
# thread.message_count — the count of messages ALREADY in the thread — straight
# into `Agent.build_phase4_prompt`, but `phase4_guidance`'s own contract is the
# ORDINAL of the reply about to be written ("This is message 12", not "message
# 11"). Combined with the system-enforced-close check firing at that SAME
# prior-count >= max_thread_messages (before any reply is generated at all),
# CONCLUDE guidance could never reach an actual reply under the default
# max_thread_messages=12: a reply only ever generates at prior-count <= 11
# (DECIDE at most), and prior-count >= 12 closes the thread as a timeout with
# no verdict, no sidecar, ever. These drive the REAL (non-mocked)
# Agent.build_phase4_prompt through a real SimulationEngine._reply_to_thread
# call — only PROFILES_DIR is faked, for hermeticity (same convention as
# tests/characterization/test_agent_turn_gm.py's _hermetic_profiles fixture).
# ---------------------------------------------------------------

def _seed_thread_history(engine, thread_id: str, channel: str, count: int) -> None:
    """Append ``count`` plain replies to ``thread_id`` so `_reply_to_thread`'s
    own recompute (``len(get_thread_history(thread_id))``) lands on exactly
    ``count`` — none of these entries' ``ts`` equals ``thread_id`` itself, so
    there is no "root" entry inflating the count by one."""
    from src.agent.message_log import LogEntry

    for i in range(count):
        engine.message_log.append(LogEntry(
            ts=f"{thread_id}-msg{i}",
            channel=channel,
            sender_agent_id="wang" if i % 2 else "blackbird",
            sender_name="WangBot" if i % 2 else "BlackbirdBot",
            content=f"message {i}",
            thread_ts=thread_id,
            posted_at=float(i),
            is_bot=True,
        ))


class TestPhase4OrdinalGuidance:
    def _engine_with_history(self, monkeypatch, tmp_path, count):
        from src.agent.agent import Agent
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
        hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            has_pending_reply=True,
        )
        hub.state.active_threads["t1"] = thread
        client = FakeSlackClient(agent_id="blackbird")
        engine = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
        _seed_thread_history(engine, "t1", "general", count)
        return engine, hub, thread, client

    @pytest.mark.asyncio
    async def test_prior_count_11_reply_gets_conclude_guidance_and_posts(
        self, monkeypatch, tmp_path,
    ):
        """The mission pin: 11 EXISTING messages -> this reply is ordinal 12
        -> MUST-CONCLUDE guidance, and the reply actually posts (the system-
        enforced-close check at prior-count 11 does not fire — 11 < 12)."""
        engine, hub, thread, client = self._engine_with_history(monkeypatch, tmp_path, 11)

        captured = {}
        real_build = hub.build_phase4_prompt

        def _spy(**kwargs):
            system, messages = real_build(**kwargs)
            captured["messages"] = messages
            return system, messages

        monkeypatch.setattr(hub, "build_phase4_prompt", _spy)

        async def _fake_generate_with_tools(**kwargs):
            return (
                "<slack_message>⏸️ Not a fit — no credible IP path here.</slack_message>"
            )

        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )

        await engine._reply_to_thread(hub, thread)

        # Must actually post — NOT silently close as a timeout with no verdict.
        assert len(client.posted) == 1
        prompt_text = captured["messages"][0]["content"]
        assert "This is message 12 — you MUST conclude the interview now" in prompt_text
        assert "**Message count:** 12 of 12 max" in prompt_text

    @pytest.mark.asyncio
    async def test_prior_count_12_thread_closes_without_generating_a_reply(
        self, monkeypatch, tmp_path,
    ):
        """The check just above the reply-generation code is unaffected by the
        ordinal fix on purpose: 12 EXISTING messages means the thread is full,
        so it closes as a timeout before the LLM is ever consulted."""
        engine, hub, thread, client = self._engine_with_history(monkeypatch, tmp_path, 12)
        monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))

        async def _fail_if_called(**kwargs):
            raise AssertionError("the LLM must not be reached once the thread is full")

        monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fail_if_called)

        await engine._reply_to_thread(hub, thread)

        assert client.posted == []
        assert thread.status == "closed"


# ---------------------------------------------------------------
# _warn_if_hub_conclude_missing_assessment — absent-sidecar detection gap
# (fix round item 2). thread_guidance.py's CONCLUDE branch is a hardcoded
# ordinal >= 12. Now that the message_count/ordinal off-by-one is fixed
# (`Agent.build_phase4_prompt` and this warning's own `phase4_guidance` call
# both feed it `thread.message_count + 1`), a reply generated when the
# thread already has 11 messages is ordinal 12 -> CONCLUDE, and — because
# the system-enforced-close check just above is a check on the unmodified
# PRIOR count (11 < 12) — that reply genuinely gets generated and posted
# under DEFAULT settings (max_thread_messages=12). No threshold inflation
# needed any more: every fixture below uses the real default.
# ---------------------------------------------------------------

class TestHubConcludeMissingAssessmentWarning:
    _WARNING_SNIPPET = "no persistable <assessment_json> sidecar was found"

    def _engine_at(self, monkeypatch, *, message_count):
        from src.agent.agent import Agent
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            has_pending_reply=True,
        )
        hub.state.active_threads["t1"] = thread
        client = FakeSlackClient(agent_id="blackbird")
        engine = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
        _seed_thread_history(engine, "t1", "general", message_count)
        monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
        return engine, hub, thread, client

    async def _drive(self, monkeypatch, engine, hub, thread, raw_response):
        async def _fake_generate_with_tools(**kwargs):
            return raw_response

        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )
        await engine._reply_to_thread(hub, thread)

    @pytest.mark.asyncio
    async def test_fires_on_conclude_non_decline_reply_with_no_sidecar(
        self, monkeypatch, caplog,
    ):
        """The mission pin: a hub reply generated at the structural CONCLUDE
        point (11 EXISTING messages -> ordinal 12, under DEFAULT settings —
        no max_thread_messages override) that neither declines nor carries a
        sidecar must warn."""
        engine, hub, thread, client = self._engine_at(monkeypatch, message_count=11)
        raw_response = (
            "<slack_message>\n"
            ":mag: Interesting, but I don't have enough to call it either way.\n"
            "</slack_message>"
        )
        with caplog.at_level("WARNING"):
            await self._drive(monkeypatch, engine, hub, thread, raw_response)

        assert len(client.posted) == 1  # confirms the reply was actually generated
        assert self._WARNING_SNIPPET in caplog.text
        assert "t1" in caplog.text
        # The warning logs the ordinal of the reply just generated (12), not
        # thread.message_count, the prior count (11) — the same off-by-one
        # that build_phase4_prompt corrects for the same reply. Logging the
        # prior count would silently mislabel every one of these warnings.
        assert "message_ordinal=12" in caplog.text
        assert "message_count=11" not in caplog.text

    @pytest.mark.asyncio
    async def test_silent_on_pause_decline_at_conclude(self, monkeypatch, caplog):
        """A ⏸️-opening decline at the CONCLUDE point is an expected,
        documented outcome (thread_guidance's "Option 2 is perfectly
        acceptable" branch) — must not warn."""
        engine, hub, thread, client = self._engine_at(monkeypatch, message_count=11)
        raw_response = (
            "<slack_message>⏸️ Not a fit — no credible IP path here.</slack_message>"
        )
        with caplog.at_level("WARNING"):
            await self._drive(monkeypatch, engine, hub, thread, raw_response)

        assert len(client.posted) == 1
        assert self._WARNING_SNIPPET not in caplog.text

    @pytest.mark.asyncio
    async def test_silent_on_non_conclude_reply_with_no_sidecar(
        self, monkeypatch, caplog,
    ):
        """Below the structural CONCLUDE point, an absent sidecar is the
        ordinary case on ~11 of every 12 turns — must stay silent (this is
        exactly what `_capture_hub_assessment`'s own docstring already
        covers; this test pins that the NEW warning does not regress it).
        8 EXISTING messages -> ordinal 9 -> still DECIDE."""
        engine, hub, thread, client = self._engine_at(monkeypatch, message_count=8)
        raw_response = (
            "<slack_message>Can you say more about the assay's throughput?</slack_message>"
        )
        with caplog.at_level("WARNING"):
            await self._drive(monkeypatch, engine, hub, thread, raw_response)

        assert len(client.posted) == 1
        assert self._WARNING_SNIPPET not in caplog.text

    @pytest.mark.asyncio
    async def test_silent_when_sidecar_present_at_conclude(self, monkeypatch, caplog):
        """A CONCLUDE reply that DOES carry a sidecar is the other
        documented, successful outcome — must not warn even though nothing
        is persisted (no database is configured in this engine)."""
        engine, hub, thread, client = self._engine_at(monkeypatch, message_count=11)
        raw_response = (
            "<slack_message>:mag: Advancing — strong differentiation.</slack_message>\n\n"
            '<assessment_json>\n'
            '{"subject_agent_id": "wang", "recommendation": "advance"}\n'
            '</assessment_json>'
        )
        with caplog.at_level("WARNING"):
            await self._drive(monkeypatch, engine, hub, thread, raw_response)

        assert len(client.posted) == 1
        assert self._WARNING_SNIPPET not in caplog.text


# ---------------------------------------------------------------
# _build_lab_directories — cohort gate must scope the "Other Labs'
# Recent Publications" section (runbook finding A3), not just the
# message log.
# ---------------------------------------------------------------

class TestBuildLabDirectoriesCohortGate:
    def _agent_with_pubs(self, agent_id, bot_name, pi_name, pub_line):
        from src.agent.agent import Agent

        agent = Agent(agent_id, bot_name, pi_name)
        agent._public_profile = (
            f"# {pi_name} Lab\n\n"
            "## Recent Publications\n"
            f"{pub_line}\n"
        )
        return agent

    def test_lab_directory_respects_the_cohort_gate(self):
        a = self._agent_with_pubs("a", "ABot", "A PI", "- A's distinctive paper on topic A")
        b = self._agent_with_pubs("b", "BBot", "B PI", "- B's distinctive paper on topic B")
        c = self._agent_with_pubs("c", "CBot", "C PI", "- C's distinctive paper on topic C")

        # Gate ON for A: may only see B.
        a.allowed_sender_ids = {"b"}
        # Gate OFF for B: sees everyone (unchanged behavior).
        b.allowed_sender_ids = None

        engine = SimulationEngine(agents=[a, b, c], slack_clients={})
        engine._build_lab_directories()

        assert "B's distinctive paper on topic B" in a._lab_directory
        assert "C's distinctive paper on topic C" not in a._lab_directory

        assert "A's distinctive paper on topic A" in b._lab_directory
        assert "C's distinctive paper on topic C" in b._lab_directory


class TestSuppressedPostBacksOff:
    """A suppressed post must not be retried forever.

    `_post_message` returning None leaves `has_pending_reply=True` so the thread
    is retried rather than silently dropped — correct, but it had no counter, so
    a thread whose reply always strips to empty burned one full-price Opus call
    every hub turn, indefinitely. Nothing else ever stopped it: a suppressed post
    writes no log row, so `thread.message_count` never advances and the
    `>= max_thread_messages` close never fires either.

    The empty-RESPONSE path next to it already backs off after 2. This gives the
    suppressed-POST path the same treatment, on its own counter so
    `empty_response_count`'s existing meaning (and the test that pins it) is
    untouched.
    """

    def _engine_with_thread(self):
        from src.agent.agent import Agent
        from src.agent.simulation import SimulationEngine
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        agent = Agent("blackbird", "BlackbirdBot", "Blackbird")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            message_count=0, has_pending_reply=True,
        )
        agent.state.active_threads["t1"] = thread
        client = FakeSlackClient(agent_id="blackbird")
        engine = SimulationEngine(agents=[agent], slack_clients={"blackbird": client})
        return engine, agent, thread

    async def _attempt(self, monkeypatch, engine, agent, thread):
        async def _fake_generate_with_tools(**kwargs):
            return "<slack_message>text that will be suppressed</slack_message>"

        async def _suppressed_post(*a, **kw):
            return None

        monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
        )
        monkeypatch.setattr(engine, "_post_message", _suppressed_post)
        await engine._reply_to_thread(agent, thread)

    @pytest.mark.asyncio
    async def test_first_suppression_still_retries(self, monkeypatch):
        engine, agent, thread = self._engine_with_thread()
        await self._attempt(monkeypatch, engine, agent, thread)
        assert thread.has_pending_reply is True
        assert thread.suppressed_post_count == 1
        assert agent.message_count == 0

    @pytest.mark.asyncio
    async def test_second_suppression_backs_off(self, monkeypatch):
        engine, agent, thread = self._engine_with_thread()
        await self._attempt(monkeypatch, engine, agent, thread)
        await self._attempt(monkeypatch, engine, agent, thread)
        assert thread.suppressed_post_count == 2
        assert thread.has_pending_reply is False, (
            "a thread whose post is always suppressed must stop being retried"
        )

    @pytest.mark.asyncio
    async def test_a_successful_post_clears_the_counter(self, monkeypatch):
        engine, agent, thread = self._engine_with_thread()
        await self._attempt(monkeypatch, engine, agent, thread)
        assert thread.suppressed_post_count == 1

        async def _ok_generate(**kwargs):
            return "<slack_message>A real reply.</slack_message>"

        monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr("src.agent.simulation.generate_with_tools", _ok_generate)
        monkeypatch.undo()
        monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr("src.agent.simulation.generate_with_tools", _ok_generate)
        await engine._reply_to_thread(agent, thread)

        assert thread.suppressed_post_count == 0
        assert thread.has_pending_reply is False


class TestMissingSidecarIsRecordedAsADrop:
    """The absent-sidecar warning must also leave a durable trace.

    `_warn_if_hub_conclude_missing_assessment` only logged. A concluding reply
    that carries no sidecar is the quietest loss of all — the reply posts, the
    thread closes, and /admin/assessments simply never gains a row. The method
    stays synchronous (its existing tests call it directly); it now reports
    whether it warned, and the async call site records the drop.
    """

    def _hub_thread(self, message_count):
        """Seed the message LOG, not just the ThreadState field.

        _reply_to_thread recomputes thread.message_count from
        message_log.get_thread_history() on entry, so setting the dataclass
        field alone is silently overwritten with 0 and the reply never reaches
        the CONCLUDE ordinal. Same helper the sibling warning tests use.
        """
        from src.agent.agent import Agent
        from src.agent.simulation import SimulationEngine
        from src.agent.state import ThreadState
        from tests.fakes import FakeSlackClient

        hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
        thread = ThreadState(
            thread_id="t1", channel="general", other_agent_id="wang",
            message_count=message_count, has_pending_reply=True,
        )
        hub.state.active_threads["t1"] = thread
        engine = SimulationEngine(
            agents=[hub],
            slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        )
        _seed_thread_history(engine, "t1", "general", message_count)
        return engine, hub, thread

    def test_it_reports_the_reason_when_the_sidecar_is_missing(self):
        # 11 prior messages -> this reply is ordinal 12 -> CONCLUDE.
        engine, hub, thread = self._hub_thread(11)
        reason = engine._warn_if_hub_conclude_missing_assessment(
            hub, thread, "A concluding reply with no sidecar.", "no sidecar here",
        )
        assert reason == "missing_sidecar"

    def test_it_reports_nothing_when_a_sidecar_is_present(self):
        engine, hub, thread = self._hub_thread(11)
        raw = '<assessment_json>{"subject_agent_id": "wang"}</assessment_json>'
        reason = engine._warn_if_hub_conclude_missing_assessment(
            hub, thread, "Concluding.", raw,
        )
        assert reason is None

    def test_it_reports_nothing_before_the_conclude_ordinal(self):
        engine, hub, thread = self._hub_thread(2)
        reason = engine._warn_if_hub_conclude_missing_assessment(
            hub, thread, "Mid-interview question.", "no sidecar",
        )
        assert reason is None

    @pytest.mark.asyncio
    async def test_the_call_site_records_the_drop(self, monkeypatch):
        """Wiring: a concluding reply with no sidecar reaches the recorder."""
        engine, hub, thread = self._hub_thread(11)
        recorded = []

        async def _fake_record(agent_id, reason, **kw):
            recorded.append((agent_id, reason, kw.get("subject_agent_id")))

        async def _fake_generate(**kwargs):
            return "<slack_message>Concluding, with no sidecar at all.</slack_message>"

        monkeypatch.setattr(engine, "_record_assessment_drop", _fake_record)
        monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
        monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_generate)

        await engine._reply_to_thread(hub, thread)

        assert ("blackbird", "missing_sidecar", "wang") in recorded


# ---------------------------------------------------------------
# Thread eviction
# ---------------------------------------------------------------


class TestEvictionIsAdditive:
    @pytest.mark.asyncio
    async def test_evicting_a_thread_does_not_unclose_it(self):
        """discard racing a concurrent close resurrects a finished interview."""
        from src.agent.agent import Agent
        from src.agent.simulation import SimulationEngine

        eng = SimulationEngine(
            agents=[Agent("wang", "WangBot", "Wang")], slack_clients={}
        )
        eng._closed_thread_ids.add("t1")
        await eng._evict_dead_thread("t1")
        assert "t1" in eng._closed_thread_ids, (
            "eviction must not remove a closed marker — Phase 3 would re-activate it"
        )
