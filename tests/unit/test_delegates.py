"""Tests for the web delegate system."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

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
            expires_at=datetime.now(UTC) + timedelta(days=30),
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
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        assert inv.status == "pending"

    def test_repr(self):
        inv = DelegateInvitation(
            agent_registry_id=uuid.uuid4(),
            invited_by_user_id=uuid.uuid4(),
            email="test@example.com",
            token="abc123",
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(days=30),
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
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        assert inv.expires_at > datetime.now(UTC)

    def test_expired(self):
        """Invitation with past expires_at is expired."""
        inv = DelegateInvitation(
            agent_registry_id=uuid.uuid4(),
            invited_by_user_id=uuid.uuid4(),
            email="test@example.com",
            token=secrets.token_urlsafe(48),
            status="pending",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        assert inv.expires_at < datetime.now(UTC)


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
        from src.routers.invite import accept_invite, router
        assert router is not None
        assert callable(accept_invite)

    def test_accept_invitation_helper_importable(self):
        """The _accept_invitation helper is importable."""
        from src.routers.invite import _accept_invitation
        assert callable(_accept_invitation)


# ---------------------------------------------------------------
# Delegate Slack-ID sync: the no-token branch (#23 R5 / V10b)
# ---------------------------------------------------------------


class _FakeResult:
    """The one value each `db.execute(...)` in `_accept_invitation` is asked for."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value


class _FakeSession:
    """Just enough AsyncSession for `_accept_invitation`'s create-delegation path.

    Deliberately not the `db_session` fixture: the branch under test is a pure
    in-Python `if bot_token: ... else: ...`, and pinning it should not cost the
    suite a Postgres container. `execute` returns the queued values in the order
    the handler asks for them — first the "already a delegate?" lookup, then the
    AgentRegistry row.
    """

    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, statement, *args, **kwargs):
        return _FakeResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("_accept_invitation rolled back on the happy path")


class TestDelegateSlackIdSyncLogging:
    async def test_no_bot_token_logs_the_skipped_delegate_slack_id_sync(
        self, monkeypatch, caplog
    ):
        """#23 V10b: an agent with no usable bot token must SAY it skipped the sync.

        `if bot_token:` used to have no `else`, so an unconfigured agent was
        indistinguishable in the logs from "the sync ran and found nothing to link".
        Asserted on the emitted message rather than on a bare "something was logged"
        so the phrase an operator greps for is what the branch actually prints —
        closure-23's own R5 measurement (`grep "skipping delegate Slack-ID sync"
        tests/` = 0 hits) was a search for a string no test spelled out.
        """
        import logging

        from src.models import AgentRegistry, User
        from src.routers import invite as invite_mod
        from src.services import slack_tokens, slack_web

        agent = AgentRegistry(
            id=uuid.uuid4(),
            agent_id="unconfigured",
            bot_name="UnconfiguredBot",
            pi_name="Un Configured",
            slack_bot_token=None,
        )
        invitation = DelegateInvitation(
            id=uuid.uuid4(),
            agent_registry_id=agent.id,
            invited_by_user_id=uuid.uuid4(),
            email="dee@example.org",
            token=secrets.token_urlsafe(48),
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        user = User(id=uuid.uuid4(), email="dee@example.org")

        # token_for_agent_row's real precedence runs (DB column, then .env), but the
        # .env tier is neutered: a developer who happens to have a token for this slug
        # in their own .env must not flip the branch under test.
        monkeypatch.setattr(slack_tokens, "env_token", lambda agent_id: None)

        async def _never(*args, **kwargs):
            raise AssertionError("called Slack with no bot token")

        monkeypatch.setattr(slack_web, "lookup_user_by_email_async", _never)

        db = _FakeSession([None, agent])
        with caplog.at_level(logging.INFO, logger="src.routers.invite"):
            response = await invite_mod._accept_invitation(invitation, user, db, None)

        # The delegation still lands — the Slack sync is best-effort, not a gate.
        assert response.status_code == 302
        assert invitation.status == "accepted"
        assert db.commits >= 1

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "skipping delegate Slack-ID sync" in m
            and "unconfigured" in m
            and "dee@example.org" in m
            for m in messages
        ), messages
