"""Tests for profile versioning."""

import uuid

from src.models.profile_revision import ProfileRevision


class TestProfileRevision:
    def test_create_revision(self):
        """ProfileRevision can be instantiated with required fields."""
        agent_id = uuid.uuid4()
        user_id = uuid.uuid4()
        rev = ProfileRevision(
            agent_registry_id=agent_id,
            profile_type="public",
            content="# Test Profile\n\nSome content.",
            changed_by_user_id=user_id,
            mechanism="web",
            change_summary="Updated research summary",
        )
        assert rev.agent_registry_id == agent_id
        assert rev.profile_type == "public"
        assert rev.content == "# Test Profile\n\nSome content."
        assert rev.changed_by_user_id == user_id
        assert rev.mechanism == "web"
        assert rev.change_summary == "Updated research summary"

    def test_create_revision_agent_initiated(self):
        """Agent-initiated revisions have no changed_by_user_id."""
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="memory",
            content="Working memory content.",
            mechanism="agent",
            change_summary="Updated after thread closure: su <> wiseman",
        )
        assert rev.changed_by_user_id is None
        assert rev.mechanism == "agent"
        assert rev.profile_type == "memory"

    def test_create_revision_pipeline(self):
        """Pipeline-generated revisions have mechanism='pipeline' and no user."""
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="public",
            content="Generated profile content.",
            mechanism="pipeline",
            change_summary="Profile generated from ORCID + PubMed",
        )
        assert rev.changed_by_user_id is None
        assert rev.mechanism == "pipeline"

    def test_create_revision_slack_dm(self):
        """Slack DM revisions track the PI who gave the instruction."""
        pi_id = uuid.uuid4()
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="private",
            content="Updated private profile.",
            changed_by_user_id=pi_id,
            mechanism="slack_dm",
            change_summary="PI instruction: prioritize aging collaborations",
        )
        assert rev.changed_by_user_id == pi_id
        assert rev.mechanism == "slack_dm"
        assert rev.profile_type == "private"

    def test_nullable_change_summary(self):
        """change_summary can be None."""
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="private",
            content="Content.",
            mechanism="web",
        )
        assert rev.change_summary is None

    def test_repr(self):
        agent_id = uuid.uuid4()
        rev = ProfileRevision(
            agent_registry_id=agent_id,
            profile_type="public",
            content="Content.",
            mechanism="web",
        )
        r = repr(rev)
        assert "public" in r
        assert "web" in r
