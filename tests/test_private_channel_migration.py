"""Tests for the public-thread → collab_private channel migration service.

Covers the pure parts of src/services/private_channels.py and the new
slack_client helpers. Full end-to-end orchestration is exercised via the
mock-mode AgentSlackClient (no real Slack, no DB writes).
"""

import pytest

from src.agent.slack_client import AgentSlackClient
from src.services.private_channels import (
    _build_handover_messages,
    _build_other_pi_dm,
    _build_slug,
)


def _join_handover(
    creator_pi_name: str,
    proposal_summary: str | None,
    guidance_text: str,
    origin_channel_name: str,
) -> str:
    """Test helper: concatenate all handover posts for content assertions."""
    return "\n---\n".join(
        _build_handover_messages(
            creator_pi_name=creator_pi_name,
            proposal_summary=proposal_summary,
            guidance_text=guidance_text,
            origin_channel_name=origin_channel_name,
        )
    )


# ---------------------------------------------------------------------------
# Slug generation (G6 — descriptive names, with trade-off accepted)
# ---------------------------------------------------------------------------


class TestSlug:
    def test_sorts_agent_ids_alphabetically(self):
        """Slug is stable regardless of which agent creates the channel."""
        a = _build_slug("wiseman", "su", "drug-repurposing")
        b = _build_slug("su", "wiseman", "drug-repurposing")
        assert a == b
        assert a == "priv-su-wiseman-drug-repurposing"

    def test_includes_origin_channel_as_topic_hint(self):
        slug = _build_slug("cravatt", "wu", "chemical-biology")
        assert slug.startswith("priv-cravatt-wu-")
        assert "chemical-biology" in slug

    def test_respects_slack_80_char_cap(self):
        """Long origin names get truncated by normalize_channel_name."""
        slug = _build_slug("su", "wiseman", "x" * 200)
        assert len(slug) <= 80

    def test_lowercase_and_hyphenated(self):
        slug = _build_slug("Su", "Wiseman", "Drug_Repurposing")
        assert slug == slug.lower()
        assert "_" not in slug


# ---------------------------------------------------------------------------
# Handover message — must contain guidance verbatim (it's the migration's
# whole point) and must NOT appear in the origin thread by construction.
# ---------------------------------------------------------------------------


class TestHandoverMessages:
    def test_contains_guidance_verbatim(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="Joint cryo-ET study of mitochondrial remodeling.",
            guidance_text="Include the unpublished HRI activator structural data.",
            origin_channel_name="structural-biology",
        )
        assert "Include the unpublished HRI activator structural data." in joined

    def test_contains_proposal_summary(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="Joint cryo-ET study of mitochondrial remodeling.",
            guidance_text="x",
            origin_channel_name="structural-biology",
        )
        assert "Joint cryo-ET study of mitochondrial remodeling." in joined

    def test_tolerates_missing_summary(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary=None,
            guidance_text="x",
            origin_channel_name="general",
        )
        assert "(no summary recorded)" in joined

    def test_references_origin_channel(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="s",
            guidance_text="g",
            origin_channel_name="drug-repurposing",
        )
        assert "#drug-repurposing" in joined

    def test_names_creator_pi(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="s",
            guidance_text="g",
            origin_channel_name="general",
        )
        assert "Andrew Su" in joined

    def test_short_handover_returns_three_posts(self):
        """Short content: [header, single guidance, closing] = 3 posts."""
        posts = _build_handover_messages(
            creator_pi_name="Andrew Su",
            proposal_summary="short summary",
            guidance_text="short guidance",
            origin_channel_name="general",
        )
        assert len(posts) == 3
        assert posts[0].startswith("*Private refinement channel*")
        assert "short guidance" in posts[1]
        assert posts[2] == "Continuing the conversation here — bots, please proceed with refinement."

    def test_long_guidance_splits_across_posts(self):
        """Long guidance exceeds per-post budget → split, with (N of M) labels."""
        long_guidance = "\n\n".join([f"Paragraph {i}: " + ("x" * 500) for i in range(10)])
        posts = _build_handover_messages(
            creator_pi_name="Andrew Su",
            proposal_summary="summary",
            guidance_text=long_guidance,
            origin_channel_name="general",
        )
        # At least 4 posts: header + ≥2 guidance chunks + closing
        assert len(posts) >= 4
        # Every post is under the length budget
        assert all(len(p) <= 3500 for p in posts)
        # Guidance chunks labeled (N of M)
        guidance_chunks = [p for p in posts if p.startswith("*Guidance from Andrew Su")]
        assert len(guidance_chunks) >= 2
        for i, chunk in enumerate(guidance_chunks, start=1):
            assert f"({i} of {len(guidance_chunks)})" in chunk

    def test_every_post_under_length_budget(self):
        """Even pathologically long inputs are clamped."""
        huge = "a" * 50000
        posts = _build_handover_messages(
            creator_pi_name="Andrew Su",
            proposal_summary=huge,
            guidance_text=huge,
            origin_channel_name="general",
        )
        assert all(len(p) <= 3500 for p in posts)


# ---------------------------------------------------------------------------
# Other-PI DM — must NOT leak the guidance text to a PI who hasn't accepted
# the channel invite yet. Their visibility to guidance content is gated by
# whether they join the private channel.
# ---------------------------------------------------------------------------


class TestOtherPIDMContent:
    def test_does_not_include_guidance_text(self):
        """The DM pointer must not embed the guidance — that lives in the
        private channel, which the PI only sees after joining."""
        dm = _build_other_pi_dm(
            other_pi_name="Luke Wiseman",
            creator_pi_name="Andrew Su",
            origin_channel_name="drug-repurposing",
            new_channel_name="priv-su-wiseman-drug-repurposing",
        )
        # Sanity: ensure the specific guidance string we use in another test
        # would never leak via this DM.
        assert "Include the unpublished HRI activator structural data." not in dm

    def test_references_both_pis_and_channels(self):
        dm = _build_other_pi_dm(
            other_pi_name="Luke Wiseman",
            creator_pi_name="Andrew Su",
            origin_channel_name="drug-repurposing",
            new_channel_name="priv-su-wiseman-drug-repurposing",
        )
        assert "Luke" in dm  # first name form is fine
        assert "Andrew Su" in dm
        assert "drug-repurposing" in dm
        assert "priv-su-wiseman-drug-repurposing" in dm


# ---------------------------------------------------------------------------
# Slack client helpers — mock mode
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """AgentSlackClient in mock mode (no real Slack)."""
    return AgentSlackClient(agent_id="su", bot_token="xoxb-placeholder-abc")


class TestCreatePrivateChannel:
    def test_returns_mock_channel_with_is_private(self, mock_client):
        ch = mock_client.create_private_channel("priv-test")
        assert ch is not None
        assert ch["name"] == "priv-test"
        assert ch["is_private"] is True
        assert ch["id"].startswith("mock_priv_")

    def test_public_create_channel_still_works(self, mock_client):
        """Don't regress the existing create_channel behavior."""
        ch = mock_client.create_channel("general")
        assert ch is not None
        assert ch["name"] == "general"
        # Mock public channels use the 'mock_' prefix (no 'priv_').
        assert ch["id"] == "mock_general"


class TestInviteToChannel:
    def test_empty_invite_list_is_noop_true(self, mock_client):
        assert mock_client.invite_to_channel("C123", []) is True

    def test_mock_mode_returns_true(self, mock_client):
        assert mock_client.invite_to_channel("C123", ["U1", "U2", "BOT3"]) is True


# ---------------------------------------------------------------------------
# Sanity: service + endpoint modules import cleanly. Catches syntax errors
# and missing deps that would otherwise only surface at request time.
# ---------------------------------------------------------------------------


class TestImports:
    def test_service_module_imports(self):
        import src.services.private_channels as svc  # noqa: F401
        assert hasattr(svc, "migrate_public_thread_to_private")
        assert hasattr(svc, "MigrationResult")

    def test_reopen_endpoint_imports(self):
        from src.routers.agent_page import reopen_proposal  # noqa: F401

    def test_config_flag_available(self):
        from src.config import get_settings
        settings = get_settings()
        assert hasattr(settings, "enable_private_refinement")
        assert isinstance(settings.enable_private_refinement, bool)
