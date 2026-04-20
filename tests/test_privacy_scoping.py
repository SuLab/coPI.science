"""Tests for G1-G3 privacy scoping: per-channel prompts, partitioned memory,
visibility-filtered deduplication. See specs/privacy-and-channel-visibility.md."""

from pathlib import Path

import pytest

from src.agent import agent as agent_module
from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine, _visibility_permits
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC


# ---------------------------------------------------------------------------
# G3: visibility ordering primitive
# ---------------------------------------------------------------------------


class TestVisibilityPermits:
    def test_public_origin_visible_everywhere(self):
        assert _visibility_permits(VISIBILITY_PUBLIC, VISIBILITY_PUBLIC) is True
        assert _visibility_permits(VISIBILITY_PUBLIC, VISIBILITY_COLLAB_PRIVATE) is True

    def test_private_origin_blocked_in_public(self):
        assert _visibility_permits(VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC) is False

    def test_private_origin_visible_in_private(self):
        assert _visibility_permits(VISIBILITY_COLLAB_PRIVATE, VISIBILITY_COLLAB_PRIVATE) is True


# ---------------------------------------------------------------------------
# G3: _get_prior_threads_for_agent filtering
# ---------------------------------------------------------------------------


class TestPriorThreadsFilter:
    """Construct a bare engine (bypassing __init__) and exercise the filter."""

    def _engine_with_threads(self, prior_threads: dict) -> SimulationEngine:
        # Bypass the heavy constructor — we only need _prior_threads for this.
        engine = SimulationEngine.__new__(SimulationEngine)
        engine._prior_threads = prior_threads
        return engine

    def test_public_context_drops_private_origins(self):
        engine = self._engine_with_threads({
            ("su", "wiseman"): [
                {"channel": "drug-repurposing", "outcome": "proposal",
                 "summary": "pub thread", "origin_visibility": VISIBILITY_PUBLIC},
                {"channel": "priv-su-wiseman", "outcome": "proposal",
                 "summary": "priv thread", "origin_visibility": VISIBILITY_COLLAB_PRIVATE},
            ]
        })
        result = engine._get_prior_threads_for_agent("su", current_visibility=VISIBILITY_PUBLIC)
        assert "wiseman" in result
        summaries = [t["summary"] for t in result["wiseman"]]
        assert summaries == ["pub thread"]

    def test_private_context_includes_both(self):
        engine = self._engine_with_threads({
            ("su", "wiseman"): [
                {"channel": "drug-repurposing", "outcome": "proposal",
                 "summary": "pub thread", "origin_visibility": VISIBILITY_PUBLIC},
                {"channel": "priv-su-wiseman", "outcome": "proposal",
                 "summary": "priv thread", "origin_visibility": VISIBILITY_COLLAB_PRIVATE},
            ]
        })
        result = engine._get_prior_threads_for_agent("su", current_visibility=VISIBILITY_COLLAB_PRIVATE)
        summaries = {t["summary"] for t in result["wiseman"]}
        assert summaries == {"pub thread", "priv thread"}

    def test_agent_with_no_threads_excluded(self):
        engine = self._engine_with_threads({
            ("su", "wiseman"): [
                {"channel": "priv", "outcome": "proposal", "summary": "x",
                 "origin_visibility": VISIBILITY_COLLAB_PRIVATE},
            ]
        })
        # In public context, the only entry is filtered out; the other agent
        # should not appear in the result (empty list would clutter the prompt).
        result = engine._get_prior_threads_for_agent("su", current_visibility=VISIBILITY_PUBLIC)
        assert result == {}

    def test_missing_origin_visibility_defaults_to_public(self):
        """Backward compat: existing rows without origin_visibility are treated as public."""
        engine = self._engine_with_threads({
            ("su", "wiseman"): [
                {"channel": "drug-repurposing", "outcome": "proposal",
                 "summary": "legacy row"},  # no origin_visibility key
            ]
        })
        result = engine._get_prior_threads_for_agent("su", current_visibility=VISIBILITY_PUBLIC)
        assert result["wiseman"][0]["summary"] == "legacy row"


# ---------------------------------------------------------------------------
# G2: partitioned working memory
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_with_tmp_profiles(tmp_path, monkeypatch):
    """Point the Agent module at a tmp profiles dir and return a fresh Agent."""
    monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
    # Pre-create the three profile subdirs so _load_file works cleanly.
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "memory").mkdir()
    return Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")


class TestPartitionedMemory:
    def test_public_memory_write_creates_partitioned_file(self, agent_with_tmp_profiles, tmp_path):
        agent_with_tmp_profiles.update_working_memory_file("public notes")
        new_path = tmp_path / "memory" / "su" / "public.md"
        assert new_path.exists()
        assert new_path.read_text().strip() == "public notes"

    def test_public_memory_write_removes_legacy_file(self, agent_with_tmp_profiles, tmp_path):
        """Writing the new partitioned layout should drop the legacy single-file form."""
        legacy = tmp_path / "memory" / "su.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("old content")
        agent_with_tmp_profiles.update_working_memory_file("fresh notes")
        assert not legacy.exists()
        assert (tmp_path / "memory" / "su" / "public.md").exists()

    def test_legacy_fallback_when_partitioned_missing(self, agent_with_tmp_profiles, tmp_path):
        """If only the legacy file exists, public_working_memory reads from it."""
        legacy = tmp_path / "memory" / "su.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("legacy body")
        assert agent_with_tmp_profiles.public_working_memory.strip() == "legacy body"

    def test_private_memory_write_goes_to_channel_specific_file(self, agent_with_tmp_profiles, tmp_path):
        agent_with_tmp_profiles.update_working_memory_file(
            "private notes",
            visibility=VISIBILITY_COLLAB_PRIVATE,
            channel_id="C123PRIV",
        )
        priv_path = tmp_path / "memory" / "su" / "private" / "C123PRIV.md"
        assert priv_path.exists()
        assert priv_path.read_text().strip() == "private notes"
        # Public segment should remain untouched.
        assert not (tmp_path / "memory" / "su" / "public.md").exists()

    def test_private_memory_requires_channel_id(self, agent_with_tmp_profiles, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.ERROR):
            agent_with_tmp_profiles.update_working_memory_file(
                "x", visibility=VISIBILITY_COLLAB_PRIVATE, channel_id=None,
            )
        assert "missing channel_id" in caplog.text

    def test_get_private_channel_memory_returns_empty_for_unknown_channel(self, agent_with_tmp_profiles):
        assert agent_with_tmp_profiles.get_private_channel_memory("NEVER_WRITTEN") == ""

    def test_private_segments_are_isolated(self, agent_with_tmp_profiles, tmp_path):
        """Each private channel's memory stays in its own file."""
        agent_with_tmp_profiles.update_working_memory_file(
            "channel A notes",
            visibility=VISIBILITY_COLLAB_PRIVATE,
            channel_id="CA",
        )
        agent_with_tmp_profiles.update_working_memory_file(
            "channel B notes",
            visibility=VISIBILITY_COLLAB_PRIVATE,
            channel_id="CB",
        )
        assert agent_with_tmp_profiles.get_private_channel_memory("CA").strip() == "channel A notes"
        assert agent_with_tmp_profiles.get_private_channel_memory("CB").strip() == "channel B notes"


# ---------------------------------------------------------------------------
# G1: per-channel prompt scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_with_memory(tmp_path, monkeypatch):
    """Agent with pre-populated public and private memory segments."""
    monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "memory" / "su" / "private").mkdir(parents=True)
    (tmp_path / "memory" / "su" / "public.md").write_text("PUBLIC_MEMORY_MARKER\n")
    (tmp_path / "memory" / "su" / "private" / "CPRIV.md").write_text("PRIVATE_MEMORY_MARKER\n")
    (tmp_path / "memory" / "su" / "private" / "COTHER.md").write_text("OTHER_PRIVATE_MARKER\n")
    # Minimal public/private profiles so the prompt builders don't emit defaults.
    (tmp_path / "public" / "su.md").write_text("public profile")
    (tmp_path / "private" / "su.md").write_text("private instructions")
    return Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")


class TestPromptScoping:
    def test_public_prompt_excludes_private_memory(self, agent_with_memory):
        prompt = agent_with_memory.build_system_prompt(visibility=VISIBILITY_PUBLIC)
        assert "PUBLIC_MEMORY_MARKER" in prompt
        assert "PRIVATE_MEMORY_MARKER" not in prompt
        assert "OTHER_PRIVATE_MARKER" not in prompt

    def test_public_prompt_omits_private_channel_rules(self, agent_with_memory):
        prompt = agent_with_memory.build_system_prompt(visibility=VISIBILITY_PUBLIC)
        assert "Private channel rules" not in prompt
        assert "still refining" not in prompt

    def test_private_prompt_includes_only_matching_channel_segment(self, agent_with_memory):
        prompt = agent_with_memory.build_system_prompt(
            visibility=VISIBILITY_COLLAB_PRIVATE, channel_id="CPRIV",
        )
        assert "PUBLIC_MEMORY_MARKER" in prompt
        assert "PRIVATE_MEMORY_MARKER" in prompt
        # Must NOT leak the other private channel's content.
        assert "OTHER_PRIVATE_MARKER" not in prompt

    def test_private_prompt_includes_rules_suffix(self, agent_with_memory):
        prompt = agent_with_memory.build_system_prompt(
            visibility=VISIBILITY_COLLAB_PRIVATE, channel_id="CPRIV",
        )
        assert "Private channel rules" in prompt
        assert "still refining" in prompt

    def test_thread_reply_prompt_respects_visibility(self, agent_with_memory):
        pub = agent_with_memory.build_thread_reply_system_prompt(visibility=VISIBILITY_PUBLIC)
        priv = agent_with_memory.build_thread_reply_system_prompt(
            visibility=VISIBILITY_COLLAB_PRIVATE, channel_id="CPRIV",
        )
        assert "PRIVATE_MEMORY_MARKER" not in pub
        assert "PRIVATE_MEMORY_MARKER" in priv
        assert "Private channel rules" not in pub
        assert "Private channel rules" in priv

    def test_default_visibility_is_public(self, agent_with_memory):
        """Existing callers that don't pass visibility should still see public-only."""
        prompt = agent_with_memory.build_system_prompt()
        assert "PUBLIC_MEMORY_MARKER" in prompt
        assert "PRIVATE_MEMORY_MARKER" not in prompt


# ---------------------------------------------------------------------------
# LogEntry visibility field
# ---------------------------------------------------------------------------


class TestLogEntryVisibility:
    def test_default_visibility_is_public(self):
        entry = LogEntry(
            ts="1", channel="general", sender_agent_id="su",
            sender_name="SuBot", content="hello",
        )
        assert entry.visibility == VISIBILITY_PUBLIC

    def test_private_visibility_explicit(self):
        entry = LogEntry(
            ts="1", channel="priv-x", sender_agent_id="su",
            sender_name="SuBot", content="hello",
            visibility=VISIBILITY_COLLAB_PRIVATE,
        )
        assert entry.visibility == VISIBILITY_COLLAB_PRIVATE
