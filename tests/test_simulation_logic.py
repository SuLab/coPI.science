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


# ---------------------------------------------------------------
# _sync_profiles_from_disk
# ---------------------------------------------------------------

class TestSyncProfilesFromDisk:
    """Per-turn reload of profiles edited from the web app (separate process)."""

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        from src.agent.agent import Agent
        import src.agent.simulation as sim

        (tmp_path / "private").mkdir()
        (tmp_path / "public").mkdir()
        priv = tmp_path / "private" / "su.md"
        priv.write_text("Focus on aging.")

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
        return engine, agent, priv, calls

    def test_first_observation_records_baseline_without_reload(self, setup):
        engine, agent, _priv, calls = setup
        engine._sync_profiles_from_disk()
        assert calls == []                                  # no reload on first pass
        assert "su" in engine._profile_mtimes              # baseline recorded

    def test_unchanged_files_do_not_reload(self, setup):
        engine, agent, _priv, calls = setup
        engine._sync_profiles_from_disk()  # baseline
        engine._sync_profiles_from_disk()  # nothing changed
        assert calls == []

    def test_external_edit_triggers_reload(self, setup):
        import os
        engine, agent, priv, calls = setup
        engine._sync_profiles_from_disk()  # baseline

        # Simulate the web app rewriting the file. Bump mtime explicitly so the
        # test is robust to sub-second filesystem timestamp resolution.
        priv.write_text("Switch focus to immunology.")
        future = engine._profile_mtimes["su"] + 10
        os.utime(priv, (future, future))

        engine._sync_profiles_from_disk()
        assert calls == [1]                                 # reloaded exactly once
        assert engine._profile_mtimes["su"] == future       # watermark advanced

        # A subsequent pass with no further change must not reload again.
        engine._sync_profiles_from_disk()
        assert calls == [1]

    def test_missing_profile_files_are_tolerated(self, setup, tmp_path):
        engine, agent, priv, calls = setup
        priv.unlink()  # no profile files on disk at all
        engine._sync_profiles_from_disk()  # must not raise
        engine._sync_profiles_from_disk()
        assert calls == []


# ---------------------------------------------------------------
# _seed_private_refinements — kick-start refinement after a reopen
# migrates a proposal into a collab_private channel.
# ---------------------------------------------------------------

class TestSeedPrivateRefinements:
    THREAD_ID = "1781124831.657319"
    CHANNEL_ID = "C0BB48ETLQL"
    CHANNEL_NAME = "priv-lairson-su-drug-repurposing-20260616-180113"
    GUIDANCE = "This needs more research. Check for knowledge graphs to augment predictions."

    def _engine_with_handover(self, *, with_handover=True, age_s=60.0):
        import time

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry

        su = Agent("su", "SuBot", "Andrew Su")
        lairson = Agent("lairson", "LairsonBot", "Brian Lairson")
        engine = SimulationEngine(agents=[su, lairson], slack_clients={})
        engine._channel_id_map[self.CHANNEL_NAME] = self.CHANNEL_ID
        engine._private_channel_members[self.CHANNEL_ID] = {"su", "lairson"}
        # Handover timestamps relative to now so the recency guard is stable
        # regardless of when the suite runs. base is `age_s` seconds ago.
        base = time.time() - age_s
        self._anchor_ts = f"{base + 2:.6f}"  # the latest of the three posts
        if with_handover:
            # Three top-level handover posts authored by the creator bot (su),
            # exactly as the web reopen flow posts them.
            for i, text in enumerate([
                "*Private refinement channel* ... *Proposal summary:* ...",
                f"*Guidance from Andrew Su:*\n{self.GUIDANCE}",
                "Continuing the conversation here — bots, please proceed with refinement.",
            ]):
                engine.message_log.append(LogEntry(
                    ts=f"{base + i:.6f}",
                    channel=self.CHANNEL_NAME,
                    sender_agent_id="su",
                    sender_name="subot",
                    content=text,
                    thread_ts=None,
                    posted_at=base + i,
                    is_bot=True,
                ))
        return engine, su, lairson

    def _migrated_info(self):
        return {self.THREAD_ID: (self.CHANNEL_ID, self.GUIDANCE)}

    def test_seeds_responder_not_last_poster(self):
        engine, su, lairson = self._engine_with_handover()
        engine._seed_private_refinements(self._migrated_info())

        # su posted the handover (last poster) → it waits, gets nothing.
        assert su.state.interesting_posts == []
        # lairson is the responder → seeded with one PI-priority post.
        assert len(lairson.state.interesting_posts) == 1
        post = lairson.state.interesting_posts[0]
        assert post.channel == self.CHANNEL_NAME
        assert post.post_id == self._anchor_ts  # the latest handover post
        assert post.pi_priority is True
        assert post.pi_context == self.GUIDANCE
        assert self.THREAD_ID in engine._db_private_refined_thread_ids

    def test_idempotent_does_not_double_seed(self):
        engine, su, lairson = self._engine_with_handover()
        engine._seed_private_refinements(self._migrated_info())
        engine._seed_private_refinements(self._migrated_info())
        assert len(lairson.state.interesting_posts) == 1

    def test_noop_when_channel_not_tracked(self):
        engine, su, lairson = self._engine_with_handover()
        engine._channel_id_map.clear()  # channel id can't resolve to a name
        engine._seed_private_refinements(self._migrated_info())
        assert lairson.state.interesting_posts == []
        # Not marked handled — must retry once the channel is tracked.
        assert self.THREAD_ID not in engine._db_private_refined_thread_ids

    def test_noop_when_handover_not_yet_in_log(self):
        engine, su, lairson = self._engine_with_handover(with_handover=False)
        engine._seed_private_refinements(self._migrated_info())
        assert lairson.state.interesting_posts == []
        # Not marked handled — self-heals on a later tick after the poll lands.
        assert self.THREAD_ID not in engine._db_private_refined_thread_ids

    def test_skips_stale_handover(self):
        # A handover older than the recency window must not be revived, but is
        # marked handled so it isn't re-evaluated every tick.
        engine, su, lairson = self._engine_with_handover(age_s=30 * 24 * 3600)
        engine._seed_private_refinements(self._migrated_info())
        assert lairson.state.interesting_posts == []
        assert self.THREAD_ID in engine._db_private_refined_thread_ids

    def test_reengages_responder_on_resume(self):
        # On resume, an active (non-finalized, recent) refinement must re-engage
        # the bot that owes a reply — even though it already participated —
        # because Phase 2 won't reliably re-surface the counterpart's last post.
        # Only the most-recent poster is held back (turn-taking).
        from src.agent.message_log import LogEntry

        engine, su, lairson = self._engine_with_handover()
        base = engine.message_log._entries[-1].posted_at
        # lairson replied, then su replied — su is the last poster; lairson owes
        # the next turn.
        for i, (aid, name) in enumerate([("lairson", "lairsonbot"), ("su", "subot")]):
            engine.message_log.append(LogEntry(
                ts=f"9999999999.00000{i}",
                channel=self.CHANNEL_NAME,
                sender_agent_id=aid,
                sender_name=name,
                content=f"Refinement reply {i} from {aid}.",
                thread_ts=None,
                posted_at=base + 1 + i,
                is_bot=True,
            ))
        assert engine.message_log.get_last_bot_sender_in_channel(self.CHANNEL_NAME) == "su"
        engine._seed_private_refinements(self._migrated_info())
        # lairson (owes the reply) is re-seeded off su's latest post; su isn't.
        assert len(lairson.state.interesting_posts) == 1
        assert lairson.state.interesting_posts[0].post_id == "9999999999.000001"
        assert su.state.interesting_posts == []

    def test_skips_finalized_channel(self):
        # A channel whose refinement already converged on a recorded proposal
        # must not be re-seeded.
        engine, su, lairson = self._engine_with_handover()
        engine._finalized_private_channels.add(self.CHANNEL_NAME)
        engine._seed_private_refinements(self._migrated_info())
        assert su.state.interesting_posts == []
        assert lairson.state.interesting_posts == []
        assert self.THREAD_ID in engine._db_private_refined_thread_ids

    def test_empty_migrated_info_is_noop(self):
        engine, su, lairson = self._engine_with_handover()
        engine._seed_private_refinements({})
        assert su.state.interesting_posts == []
        assert lairson.state.interesting_posts == []


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
# _check_private_channel_outcome / _finalize_private_proposal —
# converge a flat collab_private refinement into a revised proposal.
# ---------------------------------------------------------------

class TestPrivateChannelFinalization:
    CID = "C0BB48ETLQL"
    NAME = "priv-lairson-su-drug-repurposing-20260616-180113"
    MEMO = ":memo: Summary\n*Scientific question:* refined STING question\n*Confidence: [Moderate]*"

    def _engine(self):
        from src.agent.agent import Agent
        su = Agent("su", "SuBot", "Andrew Su")
        lairson = Agent("lairson", "LairsonBot", "Brian Lairson")
        engine = SimulationEngine(agents=[su, lairson], slack_clients={})
        engine._channel_id_map[self.NAME] = self.CID
        engine._channel_visibility[self.NAME] = "collab_private"
        engine._private_channel_members[self.CID] = {"su", "lairson"}
        return engine, su, lairson

    def _add(self, engine, sender, content, ts):
        from src.agent.message_log import LogEntry
        engine.message_log.append(LogEntry(
            ts=ts, channel=self.NAME, sender_agent_id=sender, sender_name=sender,
            content=content, thread_ts=None, posted_at=float(ts), is_bot=True,
        ))

    async def test_memo_plus_check_finalizes(self):
        engine, su, lairson = self._engine()
        self._add(engine, "lairson", self.MEMO, "100.000001")
        await engine._check_private_channel_outcome(su, self.NAME, "✅ Great — let's lock this in.")

        assert self.NAME in engine._finalized_private_channels
        for ag, other in ((su, "lairson"), (lairson, "su")):
            props = [p for p in ag.state.pending_proposals if p.thread_id == "100.000001"]
            assert len(props) == 1
            assert props[0].reviewed is False
            assert props[0].other_agent_id == other
            assert props[0].summary_text.startswith(":memo:")

    async def test_bare_memo_does_not_finalize(self):
        engine, su, lairson = self._engine()
        self._add(engine, "lairson", self.MEMO, "100.000001")
        # A :memo: with no ✅ must not finalize — it awaits the other bot's ✅.
        await engine._check_private_channel_outcome(lairson, self.NAME, self.MEMO)
        assert self.NAME not in engine._finalized_private_channels
        assert su.state.pending_proposals == []

    async def test_check_without_prior_memo_is_noop(self):
        engine, su, lairson = self._engine()
        self._add(engine, "lairson", "Some discussion, no summary yet.", "100.000001")
        await engine._check_private_channel_outcome(su, self.NAME, "✅ sounds good")
        assert self.NAME not in engine._finalized_private_channels

    async def test_check_ignores_own_memo(self):
        engine, su, lairson = self._engine()
        # su's ✅ must confirm the *other* member's memo, not su's own.
        self._add(engine, "su", self.MEMO, "100.000001")
        await engine._check_private_channel_outcome(su, self.NAME, "✅")
        assert self.NAME not in engine._finalized_private_channels

    async def test_finalization_is_idempotent(self):
        engine, su, lairson = self._engine()
        self._add(engine, "lairson", self.MEMO, "100.000001")
        await engine._check_private_channel_outcome(su, self.NAME, "✅")
        await engine._check_private_channel_outcome(su, self.NAME, "✅ again")
        # Still exactly one pending proposal per agent (no duplicate).
        assert len([p for p in su.state.pending_proposals if p.thread_id == "100.000001"]) == 1
        assert len([p for p in lairson.state.pending_proposals if p.thread_id == "100.000001"]) == 1

    async def test_handover_memo_is_not_treated_as_revised_proposal(self):
        # The handover embeds the ORIGINAL proposal summary (also :memo:). A ✅
        # before any revised summary exists must NOT finalize off the handover.
        engine, su, lairson = self._engine()
        self._add(engine, "su",
                  "*Private refinement channel*\n\n*Proposal summary:*\n" + self.MEMO,
                  "100.000001")
        await engine._check_private_channel_outcome(lairson, self.NAME, "✅ good start")
        assert self.NAME not in engine._finalized_private_channels

        # Once su posts a genuine revised summary, ✅ finalizes off that one.
        self._add(engine, "su", self.MEMO, "200.000002")
        await engine._check_private_channel_outcome(lairson, self.NAME, "✅ locking it in")
        assert self.NAME in engine._finalized_private_channels
        props = [p for p in lairson.state.pending_proposals if p.thread_id == "200.000002"]
        assert len(props) == 1


# ---------------------------------------------------------------
# mint_ts — monotonic, unique, ts-shaped ids (DB-primary store)
# ---------------------------------------------------------------

class TestMintTs:
    def test_monotonic_and_unique_under_tight_loop(self):
        engine = SimulationEngine(agents=[], slack_clients={})
        ids = [engine.mint_ts() for _ in range(1000)]
        floats = [float(x) for x in ids]
        # Strictly increasing (so posted_at=float(ts) ordering is preserved)
        assert all(b > a for a, b in zip(floats, floats[1:]))
        # All unique
        assert len(set(ids)) == len(ids)

    def test_seeded_high_water_mark_sorts_after_history(self):
        engine = SimulationEngine(agents=[], slack_clients={})
        # Simulate a rebuild that saw a far-future max(posted_at).
        engine._last_mint_ts = 9_999_999_999.0
        first = float(engine.mint_ts())
        assert first > 9_999_999_999.0
