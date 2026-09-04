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
    """Per-turn reload of profiles edited from the web app (separate process).

    _profile_mtimes stores a per-agent (sub, exists, mtime) signature rather
    than a scalar mtime max — a max can only stay the same or grow, so it
    cannot represent a deletion (#22 COR-23 / #29).
    """

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        import src.agent.simulation as sim
        from src.agent.agent import Agent

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
        future = priv.stat().st_mtime + 10
        os.utime(priv, (future, future))

        engine._sync_profiles_from_disk()
        assert calls == [1]                                 # reloaded exactly once
        assert engine._profile_mtimes["su"][0] == ("private", True, future)  # signature advanced

        # A subsequent pass with no further change must not reload again.
        engine._sync_profiles_from_disk()
        assert calls == [1]

    def test_missing_profile_files_are_tolerated(self, setup, tmp_path):
        engine, agent, priv, calls = setup
        priv.unlink()  # no profile files on disk at all
        engine._sync_profiles_from_disk()  # must not raise
        engine._sync_profiles_from_disk()
        assert calls == []

    def test_created_private_file_triggers_reload(self, setup):
        """Mirror of the deletion case below: a file appearing for the first
        time after a baseline of "absent" is also a signature change."""
        engine, agent, priv, calls = setup
        priv.unlink()  # start from a baseline where the private file is absent
        engine._sync_profiles_from_disk()  # baseline: private absent, public absent
        assert calls == []

        priv.write_text("Focus on aging.")
        engine._sync_profiles_from_disk()
        assert calls == [1]

        # A subsequent pass with no further change must not reload again.
        engine._sync_profiles_from_disk()
        assert calls == [1]

    def test_deleted_private_file_triggers_reload(self, setup):
        """Task H3's clear-after-write behaviour (commit a080900) unlinks
        profiles/private/{id}.md when a PI blanks their private instructions.
        The pre-fix scalar-mtime-max algorithm missed this: deleting the file
        drops the observed max back to the public file's mtime (or 0.0 if
        that's absent too), which is never greater than the previously
        recorded max, so `mtime > prev` stayed False and reload_profiles()
        was never called — the live agent kept serving the cached private
        instructions until restart (#22 COR-23, #29)."""
        engine, agent, priv, calls = setup
        engine._sync_profiles_from_disk()  # baseline: private present

        priv.unlink()  # the web UI's clear-after-write behaviour

        engine._sync_profiles_from_disk()
        assert calls == [1]                 # reloaded despite mtime "decreasing" to absent

        # A subsequent pass with no further change must not reload again.
        engine._sync_profiles_from_disk()
        assert calls == [1]

    def test_public_mtime_bump_still_triggers_reload(self, setup, tmp_path):
        """Unchanged behaviour: an edit to the public profile alone (no
        change to private) still reloads."""
        import os
        engine, agent, _priv, calls = setup
        pub = tmp_path / "public" / "su.md"
        pub.write_text("Public profile body.")
        engine._sync_profiles_from_disk()  # baseline: private + public present

        pub.write_text("Updated public profile body.")
        future = pub.stat().st_mtime + 10
        os.utime(pub, (future, future))

        engine._sync_profiles_from_disk()
        assert calls == [1]

        # A subsequent pass with no further change must not reload again.
        engine._sync_profiles_from_disk()
        assert calls == [1]


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
# _check_thread_outcome — a ✅ must confirm the other agent's MOST RECENT
# message, not any stale :memo: (#20 COR-3)
# ---------------------------------------------------------------

class TestCheckThreadOutcomeRequiresTheMostRecentMemo:
    """A `✅` must confirm the other agent's LATEST message, not merely the
    latest one of theirs that happens to contain `:memo:`. If they've said
    something else since (renegotiating, asking a question, anything), that
    memo is stale and today's code wrongly finalizes it anyway."""

    @pytest.fixture(autouse=True)
    def _no_live_llm_or_disk(self, monkeypatch, tmp_path):
        """_close_thread ends in _update_agent_memory, which calls the real
        Anthropic API and writes profiles/memory/<id>/public.md. Stub both:
        this is tests/unit."""
        from unittest.mock import AsyncMock

        import src.agent.agent as agent_mod
        monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value="## Working Memory\n1. nothing.\n"),
        )

    def _engine_with_thread(self):
        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.agent.state import ThreadState

        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content="kickoff", posted_at=1.0, is_bot=True,
        ))
        engine.message_log.append(LogEntry(
            ts="2.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content=":memo: Summary: proposal v1", thread_ts="1.0", posted_at=2.0, is_bot=True,
        ))
        thread = ThreadState(thread_id="1.0", channel="general", other_agent_id="b")
        return engine, a, thread

    async def test_a_later_non_memo_message_from_the_other_agent_blocks_finalization(self):
        from src.agent.message_log import LogEntry

        engine, a, thread = self._engine_with_thread()
        # b spoke again AFTER the memo, and it was not a revised memo — the
        # memo is stale, so a's later ✅ must not confirm it.
        engine.message_log.append(LogEntry(
            ts="3.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content="actually, let's use a different budget number",
            thread_ts="1.0", posted_at=3.0, is_bot=True,
        ))

        await engine._check_thread_outcome(a, thread, "Sounds great, thanks! ✅")

        assert a.state.pending_proposals == []
        assert thread.status != "closed"
        assert "1.0" not in engine._closed_thread_ids

    async def test_a_confirming_tick_right_after_the_memo_still_finalizes(self):
        # Control: the ordinary happy path (memo is genuinely the other
        # agent's latest message) must keep working.
        engine, a, thread = self._engine_with_thread()

        await engine._check_thread_outcome(a, thread, "Sounds great, thanks! ✅")

        assert len(a.state.pending_proposals) == 1
        assert a.state.pending_proposals[0].summary_text == ":memo: Summary: proposal v1"
        assert thread.status == "closed"

    async def test_a_reply_carrying_both_a_tick_and_a_memo_stays_closed(self):
        # A confirm-plus-revised-summary reply (both ✅ and :memo: in the same
        # message) must not fall through into the :memo:/⏸️ re-checks below
        # the ✅ branch and un-close (or double-close) the thread. See COR-3's
        # `break`-vs-`return` regression.
        engine, a, thread = self._engine_with_thread()

        await engine._check_thread_outcome(a, thread, "Agreed ✅ — :memo: Summary: v2")

        assert thread.status == "closed"
        assert len(engine._prior_threads[tuple(sorted(["a", "b"]))]) == 1


# ---------------------------------------------------------------
# _is_finalize_marker — shared by the public and private ✅ checks (#20 COR-4)
# ---------------------------------------------------------------

class TestFinalizeMarkerIsSharedAcrossPublicAndPrivate:
    """Both the public thread path and the private-channel path must accept the
    same two spellings of the finalize signal — today only the private path
    does; the public path matches raw ✅ only."""

    @pytest.fixture(autouse=True)
    def _no_live_llm_or_disk(self, monkeypatch, tmp_path):
        """_close_thread ends in _update_agent_memory, which calls the real
        Anthropic API and writes profiles/memory/<id>/public.md. Stub both:
        this is tests/unit. See red-team B2."""
        from unittest.mock import AsyncMock

        import src.agent.agent as agent_mod
        monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value="## Working Memory\n1. nothing.\n"),
        )

    def test_is_finalize_marker_accepts_both_forms(self):
        engine = SimulationEngine(agents=[], slack_clients={})
        assert engine._is_finalize_marker("✅") is True
        assert engine._is_finalize_marker("Sounds good :white_check_mark:") is True
        assert engine._is_finalize_marker("no marker here") is False

    async def test_public_thread_path_accepts_the_shortcode_form(self):
        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.agent.state import ThreadState

        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content="kickoff", posted_at=1.0, is_bot=True,
        ))
        engine.message_log.append(LogEntry(
            ts="2.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content=":memo: Summary: proposal v1", thread_ts="1.0", posted_at=2.0, is_bot=True,
        ))
        thread = ThreadState(thread_id="1.0", channel="general", other_agent_id="b")

        await engine._check_thread_outcome(a, thread, "Sounds great :white_check_mark:")

        assert len(a.state.pending_proposals) == 1
        assert thread.status == "closed"


# ---------------------------------------------------------------
# _close_thread — idempotent, and its dedup-context entry carries
# thread_id (#20 COR-7)
# ---------------------------------------------------------------

class TestCloseThreadIsIdempotent:
    """A second _close_thread call on the same (already-closed) ThreadState
    object must not double the ThreadDecision, the PI DM, both agents' memory
    updates, or the Phase 5 dedup-context entry."""

    @pytest.fixture(autouse=True)
    def _no_live_llm_or_disk(self, monkeypatch, tmp_path):
        """_close_thread ends in _update_agent_memory, which calls the real
        Anthropic API and writes profiles/memory/<id>/public.md. Stub both:
        this is tests/unit. See red-team B2 (both tests below drive
        _close_thread at least once)."""
        from unittest.mock import AsyncMock

        import src.agent.agent as agent_mod
        monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value="## Working Memory\n1. nothing.\n"),
        )

    def _engine_with_active_thread(self):
        from src.agent.agent import Agent
        from src.agent.state import ThreadState

        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        engine = SimulationEngine(agents=[a, b], slack_clients={})
        thread = ThreadState(thread_id="1.0", channel="general", other_agent_id="b")
        a.state.active_threads["1.0"] = thread
        return engine, a, thread

    async def test_a_second_call_on_the_same_object_does_not_duplicate(self):
        engine, a, thread = self._engine_with_active_thread()

        await engine._close_thread(a, thread, "no_proposal")
        await engine._close_thread(a, thread, "no_proposal")

        pair_key = tuple(sorted(["a", "b"]))
        assert len(engine._prior_threads[pair_key]) == 1

    async def test_the_entry_carries_thread_id(self):
        engine, a, thread = self._engine_with_active_thread()

        await engine._close_thread(a, thread, "no_proposal")

        pair_key = tuple(sorted(["a", "b"]))
        assert engine._prior_threads[pair_key][0]["thread_id"] == "1.0"


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


# ---------------------------------------------------------------
# _post_message — a text that strips to nothing must be suppressed,
# and the caller must be told.
# ---------------------------------------------------------------

class TestPostMessageSuppressesEmptyText:
    def _engine(self):
        from src.agent.agent import Agent
        su = Agent("su", "SuBot", "Andrew Su")
        # slack_clients={} puts _post_message in MOCK mode: no network, but it
        # still mints a ts and appends a LogEntry, which is what we are testing.
        return SimulationEngine(agents=[su], slack_clients={}), su

    @pytest.mark.asyncio
    async def test_text_that_strips_to_nothing_is_suppressed(self):
        engine, _su = self._engine()

        posted = await engine._post_message("su", "general", "<slack_message></slack_message>")

        assert posted is False
        assert engine.message_log._entries == []

    @pytest.mark.asyncio
    async def test_a_real_message_is_recorded_and_reports_true(self):
        engine, _su = self._engine()

        posted = await engine._post_message("su", "general", "a real message")

        assert posted is True
        assert len(engine.message_log._entries) == 1
        assert engine.message_log._entries[0].content == "a real message"


# ---------------------------------------------------------------
# _post_message — a connected client's genuinely failed post must not be
# confused with the disconnected/MOCK path (#20 COR-1b).
# ---------------------------------------------------------------

class TestPostMessageDistinguishesConnectedFailureFromMock:
    """A connected client whose chat.postMessage genuinely fails must not be
    treated like the disconnected/MOCK path — no ts minted, no LogEntry written,
    `posted` must come back False so callers don't count the turn or close threads."""

    def _engine_with_failing_client(self, error_code="msg_too_long"):
        from unittest.mock import MagicMock

        from src.agent.agent import Agent
        from src.agent.slack_client import AgentSlackClient
        from tests.fakes import slack_error

        agent = Agent("su", "SuBot", "Andrew Su")
        client = AgentSlackClient(agent_id="su", bot_token="xoxb-real-token")
        client._client = MagicMock()  # is_connected -> True (AgentSlackClient.is_connected == self._client is not None)
        client._client.chat_postMessage.side_effect = slack_error(error_code)
        engine = SimulationEngine(agents=[agent], slack_clients={"su": client})
        return engine

    @pytest.mark.asyncio
    async def test_a_swallowed_non_thread_not_found_error_returns_false_and_writes_nothing(self):
        engine = self._engine_with_failing_client("msg_too_long")

        posted = await engine._post_message("su", "general", "a real message")

        assert posted is False
        assert engine.message_log._entries == []

    @pytest.mark.asyncio
    async def test_connected_failure_is_not_confused_with_the_mock_path(self):
        # Control: the same text through a *disconnected* client (slack_clients={})
        # is the MOCK path and legitimately mints a ts + writes a row. This proves
        # the fix distinguishes on the client's connectedness, not merely on the
        # text or the error.
        from src.agent.agent import Agent
        mock_engine = SimulationEngine(agents=[Agent("su", "SuBot", "Andrew Su")], slack_clients={})
        posted = await mock_engine._post_message("su", "general", "a real message")
        assert posted is True
        assert len(mock_engine.message_log._entries) == 1

    @pytest.mark.asyncio
    async def test_thread_not_found_also_returns_false_and_evicts_the_dead_thread(self):
        # COR-1a's own fix (a reply to a deleted parent returns False rather
        # than minting a phantom entry) already landed at 18ba52c, but no
        # test pins it — this task builds the exact doubles needed to add one
        # at near-zero cost. See red-team m7 (coverage-matrix gap).
        from src.agent.message_log import LogEntry

        engine = self._engine_with_failing_client("thread_not_found")
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="other", sender_name="Other",
            content="root", posted_at=1.0, is_bot=True, slack_ts="1.0",
        ))
        evicted: list[str] = []
        engine._evict_dead_thread = evicted.append

        posted = await engine._post_message("su", "general", "a reply", thread_ts="1.0")

        assert posted is False
        assert evicted == ["1.0"]
        assert [e.ts for e in engine.message_log._entries] == ["1.0"]


# ---------------------------------------------------------------
# ProposalRef.thread_decision_id — COR-13 unified review key
# ---------------------------------------------------------------

class TestProposalRefCarriesThreadDecisionId:
    def test_default_is_none_and_a_uuid_round_trips(self):
        import uuid as uuid_mod

        from src.agent.state import ProposalRef

        bare = ProposalRef(
            thread_id="1.0", channel="general", other_agent_id="b",
            summary_text="x", proposed_at=0.0,
        )
        assert bare.thread_decision_id is None

        did = uuid_mod.uuid4()
        tagged = ProposalRef(
            thread_id="1.0", channel="general", other_agent_id="b",
            summary_text="x", proposed_at=0.0, thread_decision_id=did,
        )
        assert tagged.thread_decision_id == did


# ---------------------------------------------------------------
# _sync_proposal_reviews_from_db — the web-guidance reopen mint must be
# idempotent across restarts (COR-13 / red-team B6)
# ---------------------------------------------------------------

class TestWebGuidanceReopenMintIsIdempotentAcrossRestarts:
    """The synthetic 'PI (via web)' guidance row must be minted once, ever —
    not once per restart. Before this fix, _db_reopened_thread_ids reset to
    empty on every process start, so this whole block re-ran unconditionally
    on the first post-restart tick and re-appended another copy of the same
    guidance message forever. See COR-13 / red-team B6."""

    def _engine_with_pending_reopen(self):
        import uuid as uuid_mod
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.state import ProposalRef
        from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

        class _Row:
            def __init__(self, **kw):
                self.__dict__.update(kw)
                self.thread_decision_id = kw.get("thread_decision_id", uuid_mod.uuid4())

        rows = [_Row(
            agent_id="a", rating=0, comment="please refine the budget",
            thread_id="100.0", channel="priv-chan", refined_in_channel=None,
        )]

        class _FakeDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, *a, **kw):
                return rows

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine._channel_visibility["priv-chan"] = VISIBILITY_COLLAB_PRIVATE
        engine.session_factory = lambda: _FakeDB()
        engine.simulation_run_id = uuid_mod.uuid4()
        engine._hydrate_thread_from_db = AsyncMock()
        engine._update_agent_memory = AsyncMock()
        agent.state.pending_proposals.append(ProposalRef(
            thread_id="100.0", channel="priv-chan", other_agent_id="b",
            summary_text="x", proposed_at=0.0,
        ))
        return engine, agent

    @pytest.mark.asyncio
    async def test_a_simulated_restart_does_not_re_mint_the_guidance_row(self):
        engine, agent = self._engine_with_pending_reopen()

        await engine._sync_proposal_reviews_from_db()
        entries = [e for e in engine.message_log._entries if e.thread_ts == "100.0"]
        assert len(entries) == 1

        # Simulate a process restart: _db_reopened_thread_ids resets to empty
        # (it is in-memory only), but the message log — which a real rebuild
        # would have reloaded from the DB — keeps the previously-minted row.
        engine._db_reopened_thread_ids.clear()
        await engine._sync_proposal_reviews_from_db()

        entries_after = [e for e in engine.message_log._entries if e.thread_ts == "100.0"]
        assert len(entries_after) == 1, (
            "the synthetic PI-guidance row was re-minted on a simulated restart"
        )
        assert "100.0" in agent.state.active_threads, (
            "the ThreadState must still be (re)constructed even though the mint was skipped"
        )


# ---------------------------------------------------------------
# _check_private_channel_outcome must run only when the post landed — COR-1d
# ---------------------------------------------------------------

class TestPrivateChannelOutcomeRunsOnlyWhenPosted:
    """A suppressed Phase 5 post (COR-1b's new failure path, an empty-after-strip
    draft, or an authorship rejection) must not still trigger the private-channel
    finalization handshake — nothing was actually said in the channel."""

    def _engine(self, monkeypatch):
        from src.agent.agent import Agent
        from src.config import get_settings
        from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

        # Hermetic against a local .env with PHASE5_SKIP_PROBABILITY set
        # nonzero (red-team m7): neither test below has a pi_priority
        # candidate to bypass the random skip on its own, and both assert the
        # LLM path actually ran, which a random skip would intermittently
        # prevent under a non-default setting. Default is 0.0
        # (config.py:330), so this only guards against a developer override.
        monkeypatch.setattr(get_settings(), "phase5_skip_probability", 0.0)
        agent = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine._channel_visibility["priv-chan"] = VISIBILITY_COLLAB_PRIVATE
        return engine, agent

    def _stub_response(self):
        return (
            "```json\n"
            '{"action": "post", "channel": "priv-chan"}\n'
            "```\n"
            "<slack_message>\n"
            ":memo: Summary confirmed. ✅\n"
            "</slack_message>\n"
        )

    @pytest.mark.asyncio
    async def test_suppressed_post_does_not_check_private_outcome(self, monkeypatch):
        from unittest.mock import AsyncMock

        engine, agent = self._engine(monkeypatch)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value=self._stub_response()),
        )
        engine._post_message = AsyncMock(return_value=False)
        engine._check_private_channel_outcome = AsyncMock()

        await engine._phase5_new_post(agent)

        engine._post_message.assert_awaited_once()
        engine._check_private_channel_outcome.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_real_post_still_checks_private_outcome(self, monkeypatch):
        from unittest.mock import AsyncMock

        # Control: the guard must not swallow the legitimate case.
        engine, agent = self._engine(monkeypatch)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value=self._stub_response()),
        )
        engine._post_message = AsyncMock(return_value=True)
        engine._check_private_channel_outcome = AsyncMock()

        await engine._phase5_new_post(agent)

        engine._check_private_channel_outcome.assert_awaited_once_with(
            agent, "priv-chan", ":memo: Summary confirmed. ✅",
        )


class TestRunTurnAdvancesCursorFromTheLog:
    """The scan cursor must never advance past the newest message actually in
    the log — a wall-clock cursor can outrun a message an external writer
    (Slack human, the web app, GrantBot) posts with a lagging or skewed clock,
    filtering it out of every future scan forever. See COR-6."""

    def _engine_with_stubbed_phases(self):
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry

        agent = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="1000.0", channel="general", sender_agent_id="other", sender_name="Other",
            content="an old message", posted_at=1000.0, is_bot=True,
        ))
        # Stub every phase so _run_turn completes with no LLM/Slack/DB calls —
        # this task is only about the cursor line at the end of the method.
        engine._phase1_channel_discovery = lambda a: None
        engine._phase2_scan_filter = AsyncMock(return_value=None)
        engine._phase3_activate_threads = lambda a: None
        engine._phase4_reply_threads = AsyncMock(return_value=set())
        engine._phase5_new_post = AsyncMock(return_value=None)
        return engine, agent

    @pytest.mark.asyncio
    async def test_cursor_is_bounded_by_the_logs_latest_timestamp(self):
        engine, agent = self._engine_with_stubbed_phases()

        await engine._run_turn(agent)

        assert agent.state.last_seen_cursor == engine.message_log.latest_timestamp == 1000.0


class TestPhase3ActivationSetsMessageCountOffset:
    """A funding thread that already has 12 messages (the cap) must not
    instantly time out for an agent Phase 3 just activated into it — that
    agent gets a fresh reply budget starting from the thread's size at
    activation time, exactly like the reopen paths already do."""

    @pytest.fixture(autouse=True)
    def _hermetic_profiles(self, monkeypatch, tmp_path):
        # Driving the real _reply_to_thread below reads agent profile/memory
        # files (build_phase4_prompt -> build_thread_reply_system_prompt).
        # Point PROFILES_DIR at an empty tmp dir so nothing under the repo's
        # real profiles/ is read, and nothing is ever written there.
        monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)

    def _engine_with_capped_funding_thread(self):
        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry

        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        c = Agent("c", "CBot", "C PI")
        engine = SimulationEngine(agents=[a, b, c], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="a", sender_name="ABot",
            content=":moneybag: Funding opportunity XYZ", posted_at=1.0, is_bot=True,
        ))
        senders = ["b", "a"] * 5  # 10 replies -> 11 messages so far
        for i, sender in enumerate(senders, start=2):
            engine.message_log.append(LogEntry(
                ts=f"{i}.0", channel="general", sender_agent_id=sender, sender_name=sender,
                content=f"reply {i}", thread_ts="1.0", posted_at=float(i), is_bot=True,
            ))
        # 12th message (the cap) tags CBot — funding threads are open to all
        # (get_thread_allowed_agents returns None), so the allowed-set guard
        # does not block the new participant.
        engine.message_log.append(LogEntry(
            ts="12.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content="Let's bring in @CBot for this", thread_ts="1.0", posted_at=12.0, is_bot=True,
        ))
        return engine, c

    def _engine_with_capped_reply_thread(self):
        """Same 12-message shape as the tag-path helper, but the root is
        authored by the agent under test (CBot) and every reply comes from
        someone else — this is what makes get_replies_to_agent_posts fire
        the REPLY path in _phase3_activate_threads instead of the tag path."""
        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry

        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        c = Agent("c", "CBot", "C PI")
        engine = SimulationEngine(agents=[a, b, c], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="c", sender_name="CBot",
            content="Initial post from C", posted_at=1.0, is_bot=True,
        ))
        senders = ["b", "a"] * 5  # 10 replies -> 11 messages so far
        for i, sender in enumerate(senders, start=2):
            engine.message_log.append(LogEntry(
                ts=f"{i}.0", channel="general", sender_agent_id=sender, sender_name=sender,
                content=f"reply {i}", thread_ts="1.0", posted_at=float(i), is_bot=True,
            ))
        # 12th message (the cap) — one more reply from BBot, no tag needed:
        # CBot authored the root, so the reply path activates it for CBot.
        engine.message_log.append(LogEntry(
            ts="12.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content="One more reply", thread_ts="1.0", posted_at=12.0, is_bot=True,
        ))
        return engine, c

    async def _reply_and_assert_no_timeout_close(self, engine, agent, thread, monkeypatch):
        """Drive the real Phase-4 reply path and prove the fresh offset does
        its actual job: a newly-activated agent must NOT get an instant
        "timeout" close on a thread that was already at the cap before it
        joined. generate_with_tools is stubbed (Anthropic-free) to return an
        empty draft, which _reply_to_thread treats as an empty-response
        skip — a clean return that never reaches _post_message or
        _update_agent_memory, so stubbing those is just a belt-and-braces
        guard against a future code path change reaching them."""
        from unittest.mock import AsyncMock

        close_mock = AsyncMock()
        monkeypatch.setattr(engine, "_close_thread", close_mock)
        monkeypatch.setattr(engine, "_update_agent_memory", AsyncMock())

        async def fake_generate(**kwargs):
            return ""

        monkeypatch.setattr("src.agent.simulation.generate_with_tools", fake_generate)

        await engine._reply_to_thread(agent, thread)

        close_mock.assert_not_awaited()

    async def test_tag_path_sets_the_offset_to_the_current_count(self, monkeypatch):
        engine, c = self._engine_with_capped_funding_thread()

        engine._phase3_activate_threads(c)

        thread = c.state.active_threads["1.0"]
        assert thread.message_count_offset == 12
        # Prove the practical effect: _reply_to_thread's recompute starts
        # this newly-tagged agent at 0, not at the pre-existing 12 (which
        # would close the thread as "timeout" before it ever replied).
        await self._reply_and_assert_no_timeout_close(engine, c, thread, monkeypatch)

    async def test_reply_path_sets_the_offset_to_the_current_count(self, monkeypatch):
        engine, c = self._engine_with_capped_reply_thread()

        engine._phase3_activate_threads(c)

        thread = c.state.active_threads["1.0"]
        assert thread.message_count_offset == 12
        await self._reply_and_assert_no_timeout_close(engine, c, thread, monkeypatch)


# ---------------------------------------------------------------
# Tombstoned dead threads must not be resurrected by the inbound DB poller
# — COR-1c fix round 1 (C1)
# ---------------------------------------------------------------

class TestTombstonedThreadIsNotResurrected:
    """_evict_dead_thread purges a dead thread's log entries and adds it to
    _dead_thread_ids. Without the tombstone check, _poll_inbound_from_db's
    5-minute lookback would re-ingest a PI row for that thread (the
    dedup-by-get_entry check no longer finds the purged entry) and
    _handle_pi_inbound_entry would see the thread in _closed_thread_ids and
    re-hydrate + reopen it — resurrecting a thread whose Slack parent is gone."""

    class _FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return TestTombstonedThreadIsNotResurrected._FakeScalars(self._rows)

    class _FakeDB:
        def __init__(self, rows):
            self._rows = rows

        async def execute(self, _stmt):
            return TestTombstonedThreadIsNotResurrected._FakeResult(self._rows)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _engine_with_pi_row_on_dead_thread(self, monkeypatch, dead_ts):
        import types
        import uuid
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent

        row = types.SimpleNamespace(
            message_ts="500.000001", thread_ts=dead_ts, created_at=datetime.now(UTC),
            channel_name="general", agent_id=None, sender_name="PI su",
            content="please revisit the budget line", posted_at=500.000001,
            is_bot=False, visibility="public",
        )
        agent = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(
            agents=[agent], slack_clients={},
            session_factory=lambda: self._FakeDB([row]),
            simulation_run_id=uuid.uuid4(),
        )
        # Mirror what _evict_dead_thread would have left behind: purged from
        # the log, closed, and tombstoned.
        engine._closed_thread_ids.add(dead_ts)
        engine._dead_thread_ids.add(dead_ts)
        engine._hydrate_thread_from_db = AsyncMock()
        engine._reopen_thread = AsyncMock()
        engine._update_agent_memory = AsyncMock()
        return engine

    @pytest.mark.asyncio
    async def test_poll_inbound_from_db_skips_a_tombstoned_row(self, monkeypatch):
        dead_ts = "1776900000.000100"
        engine = self._engine_with_pi_row_on_dead_thread(monkeypatch, dead_ts)

        await engine._poll_inbound_from_db()

        assert engine.message_log.get_entry("500.000001") is None
        engine._hydrate_thread_from_db.assert_not_awaited()
        engine._reopen_thread.assert_not_awaited()
        engine._update_agent_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_pi_inbound_entry_returns_early_for_a_tombstoned_thread(self, monkeypatch):
        from src.agent.message_log import LogEntry

        dead_ts = "1776900000.000100"
        engine = self._engine_with_pi_row_on_dead_thread(monkeypatch, dead_ts)
        entry = LogEntry(
            ts="500.000001", channel="general", sender_agent_id=None,
            sender_name="PI su", content="please revisit", thread_ts=dead_ts,
            posted_at=500.000001, is_bot=False,
        )

        await engine._handle_pi_inbound_entry(entry)

        engine._hydrate_thread_from_db.assert_not_awaited()
        engine._reopen_thread.assert_not_awaited()
        engine._update_agent_memory.assert_not_awaited()


# ---------------------------------------------------------------
# _check_pi_proposal_review only clears a proposal the sender is verified
# to own, and persists a ProposalReview row — COR-5
# ---------------------------------------------------------------

class TestCheckPiProposalReviewRequiresAuthorization:
    """Only an agent in `authorized_agent_ids` may have its pending-proposal
    block cleared — the caller is responsible for proving the actual sender
    owns that agent. Without this, any message landing in the right
    thread_id clears the block for whichever agent(s) happen to be waiting
    on it, regardless of who sent it. See COR-5."""

    def _engine_with_pending_proposal(self):
        from src.agent.agent import Agent
        from src.agent.state import ProposalRef

        victim = Agent("victim", "VictimBot", "Victim PI")
        engine = SimulationEngine(agents=[victim], slack_clients={})  # no session_factory -> DB write is a no-op
        victim.state.pending_proposals.append(ProposalRef(
            thread_id="1.0", channel="general", other_agent_id="someone",
            summary_text="x", proposed_at=0.0,
        ))
        return engine, victim

    def _entry(self):
        from src.agent.message_log import LogEntry
        return LogEntry(
            ts="2.0", channel="general", sender_agent_id=None, sender_name="Random Human",
            content="anything", thread_ts="1.0", posted_at=2.0, is_bot=False,
        )

    @pytest.mark.asyncio
    async def test_an_unauthorized_sender_does_not_clear_the_block(self):
        engine, victim = self._engine_with_pending_proposal()

        await engine._check_pi_proposal_review(self._entry(), authorized_agent_ids={"attacker"})

        assert victim.state.pending_proposals[0].reviewed is False

    @pytest.mark.asyncio
    async def test_the_owning_agents_pi_does_clear_the_block(self):
        engine, victim = self._engine_with_pending_proposal()

        await engine._check_pi_proposal_review(self._entry(), authorized_agent_ids={"victim"})

        assert victim.state.pending_proposals[0].reviewed is True

    @pytest.mark.asyncio
    async def test_an_empty_authorized_set_clears_nothing(self):
        # The Slack path passes set(pi_agent_ids) verbatim — an unrecognized
        # Slack user_id (not any registered PI) yields an empty set, and this
        # must be a true no-op, not "everyone is authorized by default".
        engine, victim = self._engine_with_pending_proposal()

        await engine._check_pi_proposal_review(self._entry(), authorized_agent_ids=set())

        assert victim.state.pending_proposals[0].reviewed is False


class TestHandlePiInboundEntryDerivesAuthorizationFromThreadParticipants:
    """The DB/web path has no sender identity on the row itself, so it must
    derive the authorized set from the thread's actual participants rather
    than trusting every agent in the roster."""

    @pytest.mark.asyncio
    async def test_only_a_thread_participant_gets_unblocked(self):
        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.agent.state import ProposalRef

        participant = Agent("participant", "ParticipantBot", "Participant PI")
        bystander = Agent("bystander", "BystanderBot", "Bystander PI")
        engine = SimulationEngine(agents=[participant, bystander], slack_clients={})
        # Both happen to have a pending proposal keyed to the SAME thread_id —
        # contrived, but it isolates exactly what the participant-derivation
        # guards: only an agent who actually posted in the thread is eligible.
        for ag in (participant, bystander):
            ag.state.pending_proposals.append(ProposalRef(
                thread_id="1.0", channel="general", other_agent_id="other",
                summary_text="x", proposed_at=0.0,
            ))
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="participant", sender_name="ParticipantBot",
            content="the original proposal thread", posted_at=1.0, is_bot=True,
        ))
        pi_entry = LogEntry(
            ts="2.0", channel="general", sender_agent_id=None, sender_name="Some PI",
            content="looks good", thread_ts="1.0", posted_at=2.0, is_bot=False,
        )

        await engine._handle_pi_inbound_entry(pi_entry)

        assert participant.state.pending_proposals[0].reviewed is True
        assert bystander.state.pending_proposals[0].reviewed is False


class TestPersistImplicitProposalReview:
    """_persist_implicit_proposal_review writes a rating=-1 ProposalReview row
    keyed on the ProposalRef's own thread_decision_id (the same unified key
    _sync_proposal_reviews_from_db uses — COR-13), never overwrites an
    existing row for that (thread_decision_id, agent_id) pair, and is a
    no-op (not an error) when there is nothing to key against."""

    class _FakeResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeDB:
        def __init__(self, *, user_id, existing_review_id=None):
            self._user_id = user_id
            self._existing_review_id = existing_review_id
            self.added: list = []
            self.committed = False

        async def execute(self, stmt):
            # First call resolves AgentRegistry.user_id, second resolves the
            # existing-review check — distinguished by call order, mirroring
            # the two selects in _persist_implicit_proposal_review.
            if not hasattr(self, "_calls"):
                self._calls = 0
            self._calls += 1
            if self._calls == 1:
                return TestPersistImplicitProposalReview._FakeResult(self._user_id)
            return TestPersistImplicitProposalReview._FakeResult(self._existing_review_id)

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            self.committed = True

        async def rollback(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _CountingFactory:
        """Counts how many times the session factory itself is invoked —
        distinct from _FakeDB's internal `execute` call count — so a test can
        assert the function returned before ever opening a session, not just
        that it happened to survive whatever the session raised."""

        def __init__(self, db):
            self._db = db
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return self._db

    def _engine(self, fake_db):
        from src.agent.agent import Agent

        engine = SimulationEngine(agents=[Agent("victim", "VictimBot", "Victim PI")], slack_clients={})
        engine.session_factory = lambda: fake_db
        return engine

    @pytest.mark.asyncio
    async def test_none_thread_decision_id_is_a_no_op(self):
        # The guard must return before ever calling the session factory when
        # the caller has no decision id to key against. Asserting on a call
        # counter (rather than a factory that raises) is load-bearing: a
        # raising factory would be silently swallowed by the function's own
        # blanket `except Exception`, making the test pass even if the early
        # guard were deleted. See COR-5 fix round 1 (I1).
        from src.agent.agent import Agent

        engine = SimulationEngine(agents=[Agent("victim", "VictimBot", "Victim PI")], slack_clients={})
        factory = self._CountingFactory(db=None)
        engine.session_factory = factory

        await engine._persist_implicit_proposal_review("victim", None)

        assert factory.calls == 0

    @pytest.mark.asyncio
    async def test_a_real_decision_id_does_open_a_session(self):
        # Mirror of the no-op test above: with a real thread_decision_id and
        # a registered PI user, the factory IS invoked (exactly once).
        import uuid as uuid_mod

        user_id = uuid_mod.uuid4()
        decision_id = uuid_mod.uuid4()
        fake_db = self._FakeDB(user_id=user_id, existing_review_id=None)
        factory = self._CountingFactory(fake_db)
        engine = self._engine(fake_db)
        engine.session_factory = factory

        await engine._persist_implicit_proposal_review("victim", decision_id)

        assert factory.calls == 1

    @pytest.mark.asyncio
    async def test_writes_a_rating_minus_one_row_keyed_on_the_proposals_decision_id(self):
        import uuid as uuid_mod

        user_id = uuid_mod.uuid4()
        decision_id = uuid_mod.uuid4()
        fake_db = self._FakeDB(user_id=user_id, existing_review_id=None)
        engine = self._engine(fake_db)

        await engine._persist_implicit_proposal_review("victim", decision_id)

        assert len(fake_db.added) == 1
        row = fake_db.added[0]
        assert row.thread_decision_id == decision_id
        assert row.agent_id == "victim"
        assert row.user_id == user_id
        assert row.rating == -1
        assert row.submitted_via == "engine"
        assert fake_db.committed is True

    @pytest.mark.asyncio
    async def test_skips_the_insert_if_a_review_already_exists(self):
        import uuid as uuid_mod

        user_id = uuid_mod.uuid4()
        decision_id = uuid_mod.uuid4()
        fake_db = self._FakeDB(user_id=user_id, existing_review_id=uuid_mod.uuid4())
        engine = self._engine(fake_db)

        await engine._persist_implicit_proposal_review("victim", decision_id)

        assert fake_db.added == []
        assert fake_db.committed is False

    @pytest.mark.asyncio
    async def test_no_registered_pi_user_skips_the_write(self):
        import uuid as uuid_mod

        decision_id = uuid_mod.uuid4()
        fake_db = self._FakeDB(user_id=None)
        engine = self._engine(fake_db)

        await engine._persist_implicit_proposal_review("victim", decision_id)

        assert fake_db.added == []
        assert fake_db.committed is False


# ---------------------------------------------------------------
# _poll_pi_dms per-agent guard (COR-10(1))
# ---------------------------------------------------------------

class TestPollPiDmsGuardsPerAgent:
    """A transport error polling one agent's PI DMs (a raw socket/SSL/DNS
    error the slack_sdk re-raises unchanged — see slack_client.py's
    _call_with_retry, which only catches SlackApiError) must not kill the
    whole simulation. Both sibling pollers already guard per-item; this one
    didn't. See COR-10(1)."""

    class _RaisingDmClient:
        is_connected = True

        def poll_dm_messages(self, user_id, oldest="0"):
            raise ConnectionError("simulated transport failure")

    class _WorkingDmClient:
        is_connected = True

        def __init__(self):
            self.polled = []

        def poll_dm_messages(self, user_id, oldest="0"):
            self.polled.append((user_id, oldest))
            return []

    @pytest.mark.asyncio
    async def test_a_transport_error_for_one_agent_does_not_stop_the_poll(self):
        import uuid as uuid_mod

        from src.agent.agent import Agent

        failing = Agent("failing", "FailingBot", "Failing PI")
        working = Agent("working", "WorkingBot", "Working PI")
        working_client = self._WorkingDmClient()
        engine = SimulationEngine(
            agents=[failing, working],
            slack_clients={"failing": self._RaisingDmClient(), "working": working_client},
            session_factory=lambda: None,
            simulation_run_id=uuid_mod.uuid4(),
        )
        # Insertion order matters: "failing" must be polled BEFORE "working" so
        # a bug that propagates the exception would never reach the second
        # agent at all.
        engine._pi_slack_id_to_agent_ids = {"U_PI1": ["failing"], "U_PI2": ["working"]}

        await engine._poll_pi_dms()  # must not raise

        assert working_client.polled, "the second agent's DMs were never polled"


# ---------------------------------------------------------------
# _poll_inbound_from_db handler guard (COR-10(3))
# ---------------------------------------------------------------

class TestPollInboundFromDbGuardsTheHandler:
    """A raise inside _handle_pi_inbound_entry (e.g. handle_channel_tag's
    Slack send failing) must not crash _poll_inbound_from_db — the row is
    still appended to the log (conversation content is never lost) but the
    PI-specific side effects for that one row are logged and skipped. See
    COR-10(3)."""

    @pytest.mark.asyncio
    async def test_a_raising_handler_does_not_stop_the_poll(self, monkeypatch):
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent

        agent = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine._handle_pi_inbound_entry = AsyncMock(side_effect=ConnectionError("boom"))

        class _Row:
            created_at = None
            message_ts = "1.0"
            channel_name = "general"
            agent_id = None
            sender_name = "Some PI"
            content = "hello"
            thread_ts = None
            posted_at = 1.0
            is_bot = False
            visibility = "public"

        # Drive the per-row loop directly rather than mocking the DB query —
        # this is the exact segment the item targets and needs no session.
        # `_pi_inbox_cursor` keeps its default (EPOCH_UTC, a datetime —
        # simulation.py:412); it is subtracted from a timedelta when building
        # the WHERE clause, so overriding it with a float raises a TypeError
        # that the surrounding try/except would swallow before this test's
        # scenario is even reached.
        rows = [_Row()]

        # Monkeypatch the DB-fetch half so this stays a pure unit test; the
        # per-row processing loop below it is real, unmodified code.
        class _FakeDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, *a, **kw):
                class _R:
                    def scalars(self_inner):
                        return self_inner

                    def all(self_inner):
                        return rows

                return _R()

        engine.session_factory = lambda: _FakeDB()
        engine.simulation_run_id = "run-1"

        await engine._poll_inbound_from_db()  # must not raise

        assert engine.message_log.get_entry("1.0") is not None, (
            "the row's content must still be appended even though its "
            "PI-specific side effects failed"
        )


# ---------------------------------------------------------------
# _flush_llm_logs re-queue on failure (COR-11)
# ---------------------------------------------------------------

class TestFlushLlmLogsRequeuesOnFailure:
    """A failed flush must not drop the batch — the #30 sliding-window rate
    limiter rebuilds call_times from llm_call_logs on restart
    (_rebuild_agent_state step 4b), so a silently dropped flush under-counts
    an agent's in-window calls and lets it exceed its allowance after a
    restart. See COR-11."""

    class _FailingDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, obj):
            pass

        async def commit(self):
            raise RuntimeError("db is down")

    @pytest.mark.asyncio
    async def test_a_failed_flush_requeues_instead_of_dropping(self):
        import uuid as uuid_mod

        from src.agent.agent import Agent

        agent = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine.session_factory = lambda: self._FailingDB()
        engine.simulation_run_id = uuid_mod.uuid4()
        entry = {"agent_id": "su", "phase": "phase4", "model": "x"}
        engine._llm_log_buffer = [entry]

        await engine._flush_llm_logs()

        assert engine._llm_log_buffer == [entry]

    @pytest.mark.asyncio
    async def test_a_requeue_prepends_ahead_of_newly_buffered_entries(self):
        # Entries appended DURING the failed flush's await must stay after the
        # re-queued (older) batch, preserving chronological order — the same
        # guarantee _flush_persisted's prepend gives.
        import uuid as uuid_mod

        from src.agent.agent import Agent

        agent = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine.session_factory = lambda: self._FailingDB()
        engine.simulation_run_id = uuid_mod.uuid4()
        old_entry = {"agent_id": "su", "phase": "phase4", "model": "x", "tag": "old"}
        engine._llm_log_buffer = [old_entry]

        await engine._flush_llm_logs()
        # Simulate a new call logged after the failed flush returned.
        new_entry = {"agent_id": "su", "phase": "phase5", "model": "x", "tag": "new"}
        engine._llm_log_buffer.append(new_entry)

        assert engine._llm_log_buffer == [old_entry, new_entry]


# ---------------------------------------------------------------
# _phase5_new_post — a reply's channel must come from the target post, not
# the LLM's free-form field (#20 COR-9b)
# ---------------------------------------------------------------

class TestPhase5ReplyChannelComesFromTheTargetPost:
    """A reply's channel must be derived from the post it targets, never from
    the LLM's free-form `channel` field — otherwise a reply 'targeting' a
    collab_private post while declaring 'general' posts publicly and
    persists as public, leaking private-channel content into the public
    memory-synthesis segment. See COR-9b."""

    def _engine(self, monkeypatch):
        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.config import get_settings
        from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

        # Hermetic against a local .env with PHASE5_SKIP_PROBABILITY set
        # nonzero (red-team m7) — agent "a" has no pi_priority candidate to
        # bypass the random skip on its own, and the test below asserts the
        # LLM path actually ran. Default is 0.0 (config.py:330).
        monkeypatch.setattr(get_settings(), "phase5_skip_probability", 0.0)
        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine._channel_visibility["priv-chan"] = VISIBILITY_COLLAB_PRIVATE
        engine.message_log.append(LogEntry(
            ts="100.0", channel="priv-chan", sender_agent_id="b", sender_name="BBot",
            content="original private post", posted_at=100.0, is_bot=True,
        ))
        return engine, a

    def _public_engine(self, monkeypatch, channel="general"):
        # Control setup: same shape as _engine, but the target post lives in
        # a channel with no _channel_visibility entry — resolves to
        # VISIBILITY_PUBLIC via _resolve_channel_visibility's default. Used
        # to pin that the target-post-channel fix does not change behaviour
        # for the (overwhelmingly common) public-to-public reply case.
        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.config import get_settings

        monkeypatch.setattr(get_settings(), "phase5_skip_probability", 0.0)
        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="100.0", channel=channel, sender_agent_id="b", sender_name="BBot",
            content="original public post", posted_at=100.0, is_bot=True,
        ))
        return engine, a

    def _stub_response(self, declared_channel="general"):
        return (
            "```json\n"
            f'{{"action": "reply", "channel": "{declared_channel}", "target_post_id": "100.0"}}\n'
            "```\n"
            "<slack_message>\n"
            "Sounds interesting, let's collaborate.\n"
            "</slack_message>\n"
        )

    @pytest.mark.asyncio
    async def test_a_reply_ignores_the_llms_channel_and_uses_the_targets(self, monkeypatch):
        from unittest.mock import AsyncMock

        from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

        engine, a = self._engine(monkeypatch)
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value=self._stub_response("general")),
        )

        await engine._phase5_new_post(a)

        posts = [e for e in engine.message_log._entries if e.sender_agent_id == "a"]
        assert len(posts) == 1
        assert posts[0].channel == "priv-chan"
        # A collab_private channel is FLAT (see _phase5_new_post's private-reply
        # branch, :2554-2576): correcting the channel routes THIS reply down
        # that branch instead of the general-purpose threaded one, so it posts
        # with no thread_ts. That is the point of the fix, not an artifact of
        # the test — pre-fix this same call landed in #general, threaded onto
        # the private root's ts, and persisted visibility='public', leaking a
        # collab_private reply's content into the public memory-synthesis
        # segment. See red-team B5.
        assert posts[0].thread_ts is None
        assert posts[0].visibility == VISIBILITY_COLLAB_PRIVATE

    @pytest.mark.asyncio
    async def test_public_reply_still_threads_when_the_llm_agrees_on_channel(self, monkeypatch):
        # Control (finding I1): the target-post-channel correction must not
        # change the ordinary public-to-public reply path — a reply to a
        # public post whose LLM-declared channel matches the target's real
        # channel still threads onto the target and persists as public.
        from unittest.mock import AsyncMock

        engine, a = self._public_engine(monkeypatch, channel="general")
        engine._update_agent_memory = AsyncMock()
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value=self._stub_response("general")),
        )

        await engine._phase5_new_post(a)

        posts = [e for e in engine.message_log._entries if e.sender_agent_id == "a"]
        assert len(posts) == 1
        assert posts[0].channel == "general"
        assert posts[0].thread_ts == "100.0"
        assert posts[0].visibility == "public"

    @pytest.mark.asyncio
    async def test_public_reply_lands_in_the_targets_channel_not_the_llms_declared_channel(
        self, monkeypatch,
    ):
        # Control (finding I1): the LLM declares a DIFFERENT public channel
        # than the target's — the fix must still route to the target's real
        # channel (channel-a), not the LLM's free-form field (channel-b).
        from unittest.mock import AsyncMock

        engine, a = self._public_engine(monkeypatch, channel="channel-a")
        engine._update_agent_memory = AsyncMock()
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value=self._stub_response("channel-b")),
        )

        await engine._phase5_new_post(a)

        posts = [e for e in engine.message_log._entries if e.sender_agent_id == "a"]
        assert len(posts) == 1
        assert posts[0].channel == "channel-a"
        assert posts[0].thread_ts == "100.0"
        assert posts[0].visibility == "public"


# ---------------------------------------------------------------
# LogEntry writers that must stamp visibility from the channel, not take
# the dataclass default of 'public' (#20 COR-9a residual)
# ---------------------------------------------------------------

class TestRemainingLogEntryWritersStampVisibility:
    """_post_message already stamps visibility from the channel (COR-9a,
    fixed). These two other writers still take the LogEntry default of
    'public' regardless of the channel's real visibility class."""

    @pytest.mark.asyncio
    async def test_proposal_thread_poller_stamps_private_visibility(self):
        # async def + await, not asyncio.run — this file's convention
        # (asyncio_mode = "auto"); see red-team m7.
        from src.agent.agent import Agent
        from src.agent.state import ProposalRef
        from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

        class _StubReplyClient:
            is_connected = True
            def __init__(self, replies):
                self._replies = replies
            def get_thread_replies(self, channel_id, thread_ts, oldest="0"):
                return self._replies
            def resolve_user_name(self, user_id):
                return "Some PI"

        agent = Agent("a", "ABot", "A PI")
        stub_client = _StubReplyClient(replies=[{"ts": "200.0", "user": "U_PI", "text": "looks good"}])
        engine = SimulationEngine(agents=[agent], slack_clients={"a": stub_client})
        engine._pi_slack_id_to_agent_ids = {"U_PI": ["a"]}
        engine._channel_id_map["priv-chan"] = "C_PRIV"
        engine._channel_visibility["priv-chan"] = VISIBILITY_COLLAB_PRIVATE
        engine._private_channel_members["C_PRIV"] = {"a"}
        agent.state.pending_proposals.append(ProposalRef(
            thread_id="100.0", channel="priv-chan", other_agent_id="b",
            summary_text="x", proposed_at=0.0,
        ))

        await engine._poll_proposal_threads_for_pi()

        entry = engine.message_log.get_entry("200.0")
        assert entry is not None
        assert entry.visibility == VISIBILITY_COLLAB_PRIVATE

    @pytest.mark.asyncio
    async def test_web_reopen_synthetic_row_stamps_private_visibility(self):
        import uuid as uuid_mod
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.state import ProposalRef
        from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

        class _Row:
            def __init__(self, **kw):
                self.__dict__.update(kw)
                # Task 20.10 adds thread_decision_id to the SELECT, and this
                # task runs AFTER 20.10 in the execution order (phase 3, then
                # phase 4) — not before, so the real code already reads it
                # back via the unified (thread_decision_id, agent_id)
                # reviewed_set key (red-team m7 corrects the ordering this
                # comment originally had backwards). Giving the row a random,
                # non-None uuid here — while the ProposalRef below leaves
                # thread_decision_id at its None default — means this
                # proposal's key never matches reviewed_set, so
                # `newly_reviewed` stays empty and this path does not reach
                # _update_agent_memory on its own. The explicit stub below is
                # what actually guarantees no live call, though — don't rely
                # on the key mismatch alone (see red-team B2).
                self.thread_decision_id = kw.get("thread_decision_id", uuid_mod.uuid4())

        rows = [_Row(
            agent_id="a", rating=0, comment="please refine the budget",
            thread_id="100.0", channel="priv-chan", refined_in_channel=None,
        )]

        class _FakeDB:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False
            async def execute(self, *a, **kw):
                return rows

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine._channel_visibility["priv-chan"] = VISIBILITY_COLLAB_PRIVATE
        engine.session_factory = lambda: _FakeDB()
        engine.simulation_run_id = uuid_mod.uuid4()
        engine._hydrate_thread_from_db = AsyncMock()
        # _sync_proposal_reviews_from_db can end in a real Anthropic call via
        # _update_agent_memory -> generate_agent_response; this is tests/unit
        # and must not depend on either the network or the key-mismatch
        # reasoning above. See red-team B2.
        engine._update_agent_memory = AsyncMock()
        agent.state.pending_proposals.append(ProposalRef(
            thread_id="100.0", channel="priv-chan", other_agent_id="b",
            summary_text="x", proposed_at=0.0,
        ))

        await engine._sync_proposal_reviews_from_db()

        entries = [e for e in engine.message_log._entries if e.thread_ts == "100.0"]
        assert len(entries) == 1
        assert entries[0].visibility == VISIBILITY_COLLAB_PRIVATE


# ---------------------------------------------------------------
# A mid-turn rate check before each Phase 4/5 LLM call — E6(1)
# ---------------------------------------------------------------

class TestMidTurnRateGate:
    """The sliding-window limiter must also gate Phase 4/5 LLM calls mid-turn,
    not just at turn selection (_turn_eligible) — otherwise an agent with
    several active threads (Phase 4) or a Phase 5 call right after Phase 4
    exhausted the window can overshoot its allowance arbitrarily. See E6(1)."""

    @pytest.mark.asyncio
    async def test_phase4_caps_dispatch_to_the_rate_limit_headroom(self):
        # Fix round 1 / I1: a per-thread `_within_rate_limit()` re-check is
        # semantically all-or-nothing — nothing books a call between
        # iterations (booking only happens once a dispatched thread's
        # generate_agent_response call actually lands, well after this whole
        # batch is dispatched), so the check returns the SAME answer on every
        # pass. A probe with allowance 1 and three pending threads dispatched
        # all three under that design. The fix is a headroom slice, not a
        # per-item re-check: take exactly as many threads as the allowance
        # still has room for (allowance 1 here) and drop the rest for this
        # turn — they keep has_pending_reply=True and retry next turn.
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.state import ThreadState

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        agent.state.active_threads = {
            "1.0": ThreadState(thread_id="1.0", channel="general", other_agent_id="b", has_pending_reply=True),
            "2.0": ThreadState(thread_id="2.0", channel="general", other_agent_id="b", has_pending_reply=True),
            "3.0": ThreadState(thread_id="3.0", channel="general", other_agent_id="b", has_pending_reply=True),
        }
        engine._reply_to_thread = AsyncMock()
        engine._calls_per_load = lambda a: 1
        engine._agent_load = lambda a: 1

        await engine._phase4_reply_threads(agent)

        assert engine._reply_to_thread.await_count == 1

    @pytest.mark.asyncio
    async def test_phase4_dispatches_nothing_at_zero_allowance(self):
        # Headroom slice, not per-item re-check: allowance 0 must dispatch
        # none, not just stop partway through.
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.state import ThreadState

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        agent.state.active_threads = {
            "1.0": ThreadState(thread_id="1.0", channel="general", other_agent_id="b", has_pending_reply=True),
        }
        engine._reply_to_thread = AsyncMock()
        engine._calls_per_load = lambda a: 0
        engine._agent_load = lambda a: 1

        await engine._phase4_reply_threads(agent)

        engine._reply_to_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_phase4_still_dispatches_when_within_the_window(self):
        # Control: the gate must not block the ordinary case.
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.state import ThreadState

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        agent.state.active_threads = {
            "1.0": ThreadState(thread_id="1.0", channel="general", other_agent_id="b", has_pending_reply=True),
        }
        engine._reply_to_thread = AsyncMock()
        engine._calls_per_load = lambda a: 5
        engine._agent_load = lambda a: 1

        await engine._phase4_reply_threads(agent)

        engine._reply_to_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_phase5_skips_its_llm_call_when_the_window_is_exhausted(self, monkeypatch):
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        engine._within_rate_limit = lambda a, now: False
        fake_llm = AsyncMock(return_value='```json\n{"action": "skip"}\n```')
        monkeypatch.setattr("src.agent.simulation.generate_agent_response", fake_llm)

        await engine._phase5_new_post(agent)

        fake_llm.assert_not_awaited()


# ---------------------------------------------------------------
# A roster re-add restores state from the DB instead of a fresh, empty
# Agent — E6(2)
# ---------------------------------------------------------------

class TestRebuildOneAgentState:
    """A roster re-add previously built a bare Agent() with empty AgentState —
    the unreviewed-proposal block evaporated, active_threads/cursors/
    call_times/api_call_count all reset to zero. This restores exactly one
    agent's state from the DB + message_log, without touching anyone else's
    (see design note above for why _rebuild_agent_state() itself is unsafe
    to call mid-simulation). See E6(2)."""

    def _ordered_fake_db(self, responses):
        class _FakeDB:
            def __init__(self):
                self._responses = list(responses)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False
            async def execute(self, *a, **kw):
                payload = self._responses.pop(0)
                class _R:
                    def __init__(self_inner, payload_):
                        self_inner.payload = payload_
                    def scalars(self_inner):
                        class _S:
                            def all(self_inner2):
                                return self_inner.payload
                        return _S()
                    def all(self_inner):
                        return self_inner.payload
                    def scalar(self_inner):
                        return self_inner.payload
                    def __iter__(self_inner):
                        return iter(self_inner.payload)
                return _R(payload)
        return _FakeDB

    @pytest.mark.asyncio
    async def test_restores_proposals_calls_and_the_active_thread(self):
        import uuid as uuid_mod
        from datetime import UTC, datetime, timedelta

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry

        now = datetime.now(UTC)

        class _TD:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _LLM:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        decision = _TD(
            id=uuid_mod.uuid4(), thread_id="100.0", channel="general",
            agent_a="su", agent_b="wiseman", outcome="proposal",
            summary_text="a shared aim", decided_at=now - timedelta(minutes=5),
        )
        # 5 DB reads, in the order _rebuild_one_agent_state issues them (red-team
        # M3 — this task's original single unscoped/unwindowed LlmCallLog read
        # is corrected to the same shape _rebuild_agent_state's steps 4/4b use):
        # ThreadDecision rows, ProposalReview rows (none yet — unreviewed), the
        # (thread_id, reopened_at) rows for the B6 restoration (none reopened here),
        # an all-time-scoped-to-this-run COUNT(*) scalar, and a SEPARATE windowed
        # query — which a real DB would already have filtered to just the
        # in-window row, so the fake's payload reflects that, not a raw dump
        # of every row for later Python-side filtering.
        responses = [
            [decision],
            [],
            [],
            2,
            [_LLM(created_at=now - timedelta(seconds=10))],
        ]
        FakeDB = self._ordered_fake_db(responses)

        su = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(agents=[su], slack_clients={})
        engine.session_factory = FakeDB
        engine.simulation_run_id = uuid_mod.uuid4()
        engine.message_log.append(LogEntry(
            ts="100.0", channel="general", sender_agent_id="su", sender_name="SuBot",
            content="root post", posted_at=100.0, is_bot=True,
        ))
        engine.message_log.append(LogEntry(
            ts="200.0", channel="general", sender_agent_id="wiseman", sender_name="WisemanBot",
            content="a reply", thread_ts="100.0", posted_at=200.0, is_bot=True,
        ))

        await engine._rebuild_one_agent_state("su")

        assert len(su.state.pending_proposals) == 1
        assert su.state.pending_proposals[0].thread_id == "100.0"
        assert su.state.pending_proposals[0].reviewed is False
        assert su.api_call_count == 2
        assert len(su.state.call_times) == 1  # only the in-window call
        assert "100.0" in su.state.active_threads
        assert su.state.last_seen_cursor == engine.message_log.latest_timestamp == 200.0

    @pytest.mark.asyncio
    async def test_a_bystander_agent_is_left_untouched(self):
        # Fix round 1 / I2: every read AND the only write that matters
        # (last_seen_cursor) must be scoped to the single agent_id being
        # rebuilt. A second, unrelated agent's cursor, call_times and
        # pending_proposals must come out exactly as they went in.
        import uuid as uuid_mod
        from datetime import UTC, datetime, timedelta

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.agent.state import ProposalRef

        now = datetime.now(UTC)

        class _TD:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class _LLM:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        decision = _TD(
            id=uuid_mod.uuid4(), thread_id="100.0", channel="general",
            agent_a="su", agent_b="wiseman", outcome="proposal",
            summary_text="a shared aim", decided_at=now - timedelta(minutes=5),
        )
        responses = [
            [decision],
            [],
            [],
            2,
            [_LLM(created_at=now - timedelta(seconds=10))],
        ]
        FakeDB = self._ordered_fake_db(responses)

        su = Agent("su", "SuBot", "Andrew Su")
        wiseman = Agent("wiseman", "WisemanBot", "Wiseman PI")
        wiseman.state.last_seen_cursor = 999.0
        wiseman.state.call_times.append(now.timestamp())
        wiseman.state.pending_proposals.append(ProposalRef(
            thread_id="500.0", channel="general", other_agent_id="su",
            summary_text="a bystander's own proposal", proposed_at=0.0,
        ))
        bystander_cursor = wiseman.state.last_seen_cursor
        bystander_call_times = list(wiseman.state.call_times)
        bystander_proposals = list(wiseman.state.pending_proposals)

        engine = SimulationEngine(agents=[su, wiseman], slack_clients={})
        engine.session_factory = FakeDB
        engine.simulation_run_id = uuid_mod.uuid4()
        engine.message_log.append(LogEntry(
            ts="100.0", channel="general", sender_agent_id="su", sender_name="SuBot",
            content="root post", posted_at=100.0, is_bot=True,
        ))
        engine.message_log.append(LogEntry(
            ts="200.0", channel="general", sender_agent_id="wiseman", sender_name="WisemanBot",
            content="a reply", thread_ts="100.0", posted_at=200.0, is_bot=True,
        ))

        await engine._rebuild_one_agent_state("su")

        assert wiseman.state.last_seen_cursor == bystander_cursor
        assert list(wiseman.state.call_times) == bystander_call_times
        assert wiseman.state.pending_proposals == bystander_proposals

    @pytest.mark.asyncio
    async def test_a_closed_thread_is_not_restored_into_active_threads(self):
        # In production the startup rebuild puts every decided thread into
        # _closed_thread_ids (:4166-4186) before a roster re-add can ever
        # happen — the test above never exercises that guard (its thread
        # starts with _closed_thread_ids empty, a combination production
        # cannot reach). See red-team m6.
        import uuid as uuid_mod

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry

        FakeDB = self._ordered_fake_db([[], [], [], 0, []])   # 5 reads: decisions, reviews, reopened, count, window

        su = Agent("su", "SuBot", "Andrew Su")
        engine = SimulationEngine(agents=[su], slack_clients={})
        engine.session_factory = FakeDB
        engine.simulation_run_id = uuid_mod.uuid4()
        engine._closed_thread_ids.add("300.0")
        engine.message_log.append(LogEntry(
            ts="300.0", channel="general", sender_agent_id="su", sender_name="SuBot",
            content="a decided root", posted_at=300.0, is_bot=True,
        ))
        engine.message_log.append(LogEntry(
            ts="301.0", channel="general", sender_agent_id="wiseman", sender_name="WisemanBot",
            content="a reply", thread_ts="300.0", posted_at=301.0, is_bot=True,
        ))

        await engine._rebuild_one_agent_state("su")

        assert "300.0" not in su.state.active_threads


# ---------------------------------------------------------------
# The daily post cap must not block a PI-priority, funding, or
# private-channel candidate — #20 E7a
# ---------------------------------------------------------------

class TestDailyCapDoesNotBlockBypassEligibleCandidates:
    """PI-priority, funding, and private-channel candidates already bypass the
    random skip and the unreviewed-proposal block a few lines later in this
    same function — the daily post cap must not be the one gate that still
    blocks them outright. See E7a."""

    def _engine_at_cap(self):
        import time

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.config import get_settings

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        cap = get_settings().daily_post_cap
        now = time.time()
        for i in range(cap):
            engine.message_log.append(LogEntry(
                ts=f"{now + i}.0", channel="general", sender_agent_id="a", sender_name="ABot",
                content=f"post {i}", posted_at=now + i, is_bot=True,
            ))
        return engine, agent

    @pytest.mark.asyncio
    async def test_a_pi_priority_candidate_bypasses_the_cap(self, monkeypatch):
        from unittest.mock import AsyncMock

        from src.agent.state import PostRef

        engine, agent = self._engine_at_cap()
        agent.state.interesting_posts.append(PostRef(
            post_id="999.0", channel="general", sender_agent_id="b",
            content_snippet="x", posted_at=0.0, pi_priority=True,
        ))
        fake_llm = AsyncMock(return_value='```json\n{"action": "skip"}\n```')
        monkeypatch.setattr("src.agent.simulation.generate_agent_response", fake_llm)

        await engine._phase5_new_post(agent)

        fake_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_ordinary_candidate_still_hits_the_cap(self, monkeypatch):
        # Control: the cap must still apply to a plain, non-exempt candidate.
        from unittest.mock import AsyncMock

        from src.agent.state import PostRef

        engine, agent = self._engine_at_cap()
        agent.state.interesting_posts.append(PostRef(
            post_id="999.0", channel="general", sender_agent_id="b",
            content_snippet="x", posted_at=0.0,
        ))
        fake_llm = AsyncMock(return_value='```json\n{"action": "skip"}\n```')
        monkeypatch.setattr("src.agent.simulation.generate_agent_response", fake_llm)

        await engine._phase5_new_post(agent)

        fake_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_funding_candidate_does_not_make_the_cap_unenforceable_for_an_ordinary_post(
        self, monkeypatch,
    ):
        # The bypass at the top of the method is keyed on interesting_posts
        # CONTAINING a bypass-eligible candidate, not on the LLM actually
        # choosing it, so the LLM still gets called here — the point is that
        # the ordinary top-level post it chooses must not actually land.
        # Without the downstream re-check, one funding post sitting in
        # interesting_posts would make the cap unenforceable for every OTHER
        # action too.
        from unittest.mock import AsyncMock

        from src.agent.state import PostRef

        engine, agent = self._engine_at_cap()
        agent.state.interesting_posts.append(PostRef(
            post_id="999.0", channel="funding-opportunities", sender_agent_id="grantbot",
            content_snippet="x", posted_at=0.0,
        ))
        engine.message_log.is_funding_thread = lambda post_id: post_id == "999.0"
        response = (
            "```json\n"
            '{"action": "post", "channel": "general"}\n'
            "```\n"
            "<slack_message>\n"
            "An ordinary top-level post.\n"
            "</slack_message>\n"
        )
        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response",
            AsyncMock(return_value=response),
        )
        posts_before = len(engine.message_log._entries)

        await engine._phase5_new_post(agent)

        assert len(engine.message_log._entries) == posts_before, (
            "an ordinary post landed even though the daily cap was reached and "
            "the chosen action was not itself bypass-eligible"
        )

    @pytest.mark.asyncio
    async def test_a_funding_candidate_already_in_active_threads_does_not_unlock_the_cap(
        self, monkeypatch,
    ):
        # A funding candidate the agent already replied to (Phase 4) sits in
        # active_threads and is filtered out of available_posts by the
        # availability loop above — it must not unlock the cap for an
        # otherwise-capped turn, or the agent burns an Opus call every turn
        # for the rest of the day only for the post-LLM re-check to reject
        # whatever the LLM chooses. See fix round 1, I1.
        from unittest.mock import AsyncMock

        from src.agent.message_log import LogEntry
        from src.agent.state import PostRef, ThreadState

        engine, agent = self._engine_at_cap()
        engine.message_log.append(LogEntry(
            ts="999.0", channel="funding-opportunities", sender_agent_id="grantbot",
            sender_name="GrantBot", content="New funding opportunity :moneybag:",
            posted_at=0.0, is_bot=True,
        ))
        agent.state.interesting_posts.append(PostRef(
            post_id="999.0", channel="funding-opportunities", sender_agent_id="grantbot",
            content_snippet="x", posted_at=0.0,
        ))
        agent.state.active_threads["999.0"] = ThreadState(
            thread_id="999.0", channel="funding-opportunities", other_agent_id="grantbot",
        )
        fake_llm = AsyncMock(return_value='```json\n{"action": "skip"}\n```')
        monkeypatch.setattr("src.agent.simulation.generate_agent_response", fake_llm)

        await engine._phase5_new_post(agent)

        fake_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_funding_candidate_handled_in_phase4_does_not_unlock_the_cap(
        self, monkeypatch,
    ):
        # Same as above, but the candidate was handled this very turn (Phase
        # 4 already replied to it), so it arrives via phase4_thread_ids
        # instead of active_threads.
        from unittest.mock import AsyncMock

        from src.agent.message_log import LogEntry
        from src.agent.state import PostRef

        engine, agent = self._engine_at_cap()
        engine.message_log.append(LogEntry(
            ts="999.0", channel="funding-opportunities", sender_agent_id="grantbot",
            sender_name="GrantBot", content="New funding opportunity :moneybag:",
            posted_at=0.0, is_bot=True,
        ))
        agent.state.interesting_posts.append(PostRef(
            post_id="999.0", channel="funding-opportunities", sender_agent_id="grantbot",
            content_snippet="x", posted_at=0.0,
        ))
        fake_llm = AsyncMock(return_value='```json\n{"action": "skip"}\n```')
        monkeypatch.setattr("src.agent.simulation.generate_agent_response", fake_llm)

        await engine._phase5_new_post(agent, phase4_thread_ids={"999.0"})

        fake_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_actionable_funding_candidate_still_bypasses_the_cap(self, monkeypatch):
        # Control: the same funding candidate, still actionable (not already
        # handled this turn or a prior one), must still bypass the cap.
        from unittest.mock import AsyncMock

        from src.agent.message_log import LogEntry
        from src.agent.state import PostRef

        engine, agent = self._engine_at_cap()
        engine.message_log.append(LogEntry(
            ts="999.0", channel="funding-opportunities", sender_agent_id="grantbot",
            sender_name="GrantBot", content="New funding opportunity :moneybag:",
            posted_at=0.0, is_bot=True,
        ))
        agent.state.interesting_posts.append(PostRef(
            post_id="999.0", channel="funding-opportunities", sender_agent_id="grantbot",
            content_snippet="x", posted_at=0.0,
        ))
        fake_llm = AsyncMock(return_value='```json\n{"action": "skip"}\n```')
        monkeypatch.setattr("src.agent.simulation.generate_agent_response", fake_llm)

        await engine._phase5_new_post(agent)

        fake_llm.assert_awaited_once()


# ---------------------------------------------------------------
# has_pi_directive must survive a turn where Phase 5 ran but made no
# LLM call — #20 E7b
# ---------------------------------------------------------------

class TestHasPiDirectiveClearedOnlyWhenActedOn:
    """The flag must survive a turn where Phase 5 ran (because of it) but made
    no LLM call — otherwise the directive that triggered Phase 5 is dropped
    without ever influencing a prompt. See E7b."""

    def _engine_with_stubbed_phases(self, phase5_makes_a_call: bool):
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})
        agent.state.has_pi_directive = True
        engine._phase1_channel_discovery = lambda a: None
        engine._phase2_scan_filter = AsyncMock(return_value=None)
        engine._phase3_activate_threads = lambda a: None
        engine._phase4_reply_threads = AsyncMock(return_value=set())

        async def _phase5(agent_arg, phase4_thread_ids=None):
            if phase5_makes_a_call:
                agent_arg.record_api_call()  # simulates a real LLM call attempt

        engine._phase5_new_post = _phase5
        return engine, agent

    @pytest.mark.asyncio
    async def test_flag_survives_a_turn_where_phase5_made_no_call(self):
        engine, agent = self._engine_with_stubbed_phases(phase5_makes_a_call=False)

        await engine._run_turn(agent)

        assert agent.state.has_pi_directive is True

    @pytest.mark.asyncio
    async def test_flag_clears_once_phase5_actually_calls_the_llm(self):
        engine, agent = self._engine_with_stubbed_phases(phase5_makes_a_call=True)

        await engine._run_turn(agent)

        assert agent.state.has_pi_directive is False


# ---------------------------------------------------------------
# thread.pi_context must not survive past the one prompt it was
# injected into — #20 E7c
# ---------------------------------------------------------------

class TestPiContextClearedAfterInjection:
    """thread.pi_context must not survive past the one prompt it was injected
    into — otherwise it is re-injected as 'authoritative' into every future
    reply on the thread. See E7c."""

    @pytest.fixture(autouse=True)
    def _hermetic_profiles(self, monkeypatch, tmp_path):
        # Driving the real _reply_to_thread below reads agent profile/memory
        # files (build_phase4_prompt -> build_thread_reply_system_prompt).
        # Point PROFILES_DIR at an empty tmp dir so nothing under the repo's
        # real profiles/ is read, and nothing is ever written there.
        monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)

    @pytest.mark.asyncio
    async def test_pi_context_is_cleared_after_the_reply_attempt(self, monkeypatch):
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.agent.state import ThreadState

        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content="root", posted_at=1.0, is_bot=True,
        ))
        thread = ThreadState(
            thread_id="1.0", channel="general", other_agent_id="b",
            pi_context="please look at X",
        )
        # A response with no <slack_message> tags -> _extract_slack_message
        # yields empty -> the reply is never posted. The clear must still
        # have happened, since injection (not posting) is what this task
        # gates on.
        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools",
            AsyncMock(return_value="no tags here"),
        )

        await engine._reply_to_thread(a, thread)

        assert thread.pi_context is None

    @pytest.mark.asyncio
    async def test_pi_context_survives_a_transport_error_on_the_llm_call(self, monkeypatch):
        # Phase 4 dispatches _reply_to_thread inside
        # asyncio.gather(*tasks, return_exceptions=True) (:1332) — an error
        # here is swallowed by the caller. If the clear ran before
        # generate_with_tools (as an earlier draft of this task had it), the
        # guidance would be lost having reached no prompt at all. The
        # corrected placement (right after the call succeeds) must leave
        # pi_context untouched here, for a later turn to retry. See red-team m4.
        from unittest.mock import AsyncMock

        from src.agent.agent import Agent
        from src.agent.message_log import LogEntry
        from src.agent.state import ThreadState

        a = Agent("a", "ABot", "A PI")
        b = Agent("b", "BBot", "B PI")
        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine.message_log.append(LogEntry(
            ts="1.0", channel="general", sender_agent_id="b", sender_name="BBot",
            content="root", posted_at=1.0, is_bot=True,
        ))
        thread = ThreadState(
            thread_id="1.0", channel="general", other_agent_id="b",
            pi_context="please look at X",
        )
        monkeypatch.setattr(
            "src.agent.simulation.generate_with_tools",
            AsyncMock(side_effect=RuntimeError("transport error")),
        )

        await engine._reply_to_thread(a, thread)

        assert thread.pi_context == "please look at X"


# ---------------------------------------------------------------
# The interesting_posts swap during Phase-5 prompt-building must be
# restored even if prompt-building raises — #20 E7d
# ---------------------------------------------------------------

class TestInterestingPostsRestoredOnException:
    """A raise anywhere between the interesting_posts swap and its restore
    must not leave the agent permanently narrowed to that turn's filtered
    subset. See E7d."""

    @pytest.mark.asyncio
    async def test_a_raise_during_prompt_building_still_restores_the_full_list(self):
        from src.agent.agent import Agent
        from src.agent.state import PostRef, ThreadState
        from src.config import get_settings

        agent = Agent("a", "ABot", "A PI")
        engine = SimulationEngine(agents=[agent], slack_clients={})

        # Block the agent so a non-priority post is excluded from
        # available_posts, making the swap a STRICT subset of the original —
        # otherwise this turn's filtered list can coincidentally equal the
        # full one and the test would pass for the wrong reason.
        threshold = get_settings().active_thread_threshold
        for i in range(threshold):
            agent.state.active_threads[f"t{i}"] = ThreadState(
                thread_id=f"t{i}", channel="general", other_agent_id="b", status="active",
            )
        orig_ineligible = PostRef(
            post_id="orig.0", channel="general", sender_agent_id="b",
            content_snippet="x", posted_at=0.0,
        )
        avail_eligible = PostRef(
            post_id="avail.0", channel="general", sender_agent_id="b",
            content_snippet="y", posted_at=1.0, pi_priority=True,
        )
        agent.state.interesting_posts = [orig_ineligible, avail_eligible]

        def _raise(*a, **kw):
            raise RuntimeError("boom in prompt building")

        engine._get_prior_threads_for_agent = _raise

        with pytest.raises(RuntimeError):
            await engine._phase5_new_post(agent)

        assert [p.post_id for p in agent.state.interesting_posts] == ["orig.0", "avail.0"]
