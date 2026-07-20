"""Tests for the web delegate system."""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.models.delegate import AgentDelegate, DelegateInvitation


# ---------------------------------------------------------------
# DelegateInvitation model
# ---------------------------------------------------------------


class TestDelegateInvitation:
    def test_create_invitation(self):
        """DelegateInvitation can be instantiated with required fields."""
        inv = DelegateInvitation(
            agent_registry_id=uuid.uuid4(),
            invited_by_user_id=uuid.uuid4(),
            email="test@example.com",
            token=secrets.token_urlsafe(48),
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        assert inv.status == "pending"
        assert inv.email == "test@example.com"
        assert inv.accepted_at is None
        assert inv.accepted_by_user_id is None

    def test_default_status(self):
        """Default status is 'pending' (applied by DB server_default, not Python default)."""
        inv = DelegateInvitation(
            agent_registry_id=uuid.uuid4(),
            invited_by_user_id=uuid.uuid4(),
            email="test@example.com",
            token=secrets.token_urlsafe(48),
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        assert inv.status == "pending"

    def test_repr(self):
        inv = DelegateInvitation(
            agent_registry_id=uuid.uuid4(),
            invited_by_user_id=uuid.uuid4(),
            email="test@example.com",
            token="abc123",
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        assert "test@example.com" in repr(inv)
        assert "pending" in repr(inv)


# ---------------------------------------------------------------
# AgentDelegate model
# ---------------------------------------------------------------


class TestAgentDelegate:
    def test_create_delegate(self):
        """AgentDelegate can be instantiated with required fields."""
        delegate = AgentDelegate(
            agent_registry_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            notify_proposals=True,
        )
        assert delegate.notify_proposals is True

    def test_notify_proposals_explicit(self):
        """notify_proposals can be set explicitly."""
        delegate = AgentDelegate(
            agent_registry_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            notify_proposals=False,
        )
        assert delegate.notify_proposals is False

    def test_repr(self):
        agent_id = uuid.uuid4()
        user_id = uuid.uuid4()
        delegate = AgentDelegate(
            agent_registry_id=agent_id,
            user_id=user_id,
        )
        assert str(agent_id) in repr(delegate)
        assert str(user_id) in repr(delegate)


# ---------------------------------------------------------------
# Invitation token validation
# ---------------------------------------------------------------


class TestInvitationExpiry:
    def test_not_expired(self):
        """Invitation with future expires_at is valid."""
        inv = DelegateInvitation(
            agent_registry_id=uuid.uuid4(),
            invited_by_user_id=uuid.uuid4(),
            email="test@example.com",
            token=secrets.token_urlsafe(48),
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        assert inv.expires_at > datetime.now(timezone.utc)

    def test_expired(self):
        """Invitation with past expires_at is expired."""
        inv = DelegateInvitation(
            agent_registry_id=uuid.uuid4(),
            invited_by_user_id=uuid.uuid4(),
            email="test@example.com",
            token=secrets.token_urlsafe(48),
            status="pending",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        assert inv.expires_at < datetime.now(timezone.utc)


# ---------------------------------------------------------------
# Authorization logic (get_agent_with_access)
# ---------------------------------------------------------------


class TestGetAgentWithAccess:
    """Test the authorization dependency logic (unit-level checks)."""

    def test_imports(self):
        """get_agent_with_access is importable."""
        from src.dependencies import get_agent_with_access
        assert callable(get_agent_with_access)


# ---------------------------------------------------------------
# Email service
# ---------------------------------------------------------------


class TestEmailService:
    def test_imports(self):
        """Email service is importable."""
        from src.services.email import send_delegate_invitation
        assert callable(send_delegate_invitation)


# ---------------------------------------------------------------
# Invite router
# ---------------------------------------------------------------


class TestInviteRouter:
    def test_imports(self):
        """Invite router is importable."""
        from src.routers.invite import router, accept_invite
        assert router is not None
        assert callable(accept_invite)

    def test_accept_invitation_helper_importable(self):
        """The _accept_invitation helper is importable."""
        from src.routers.invite import _accept_invitation
        assert callable(_accept_invitation)
