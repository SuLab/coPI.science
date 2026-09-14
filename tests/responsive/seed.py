"""Seed the responsive-tier database.

Opens its own committing session against ``pg_url`` (the tier runs the server as a
subprocess, so it cannot share the test process's rolled-back transaction). Every
row created here is deleted by :func:`teardown`, which truncates exactly the
tables this module writes to.

Mirrors ``tests/e2e/seed.py``'s shape (committing, real ids) but is not
idempotent — the responsive tier owns a scratch database for the run and tears
down what it created rather than reconciling against existing rows.
"""

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.models import (
    AccessAllowlist,
    AgentDelegate,
    Cohort,
    CohortMembership,
    DelegateInvitation,
    Job,
    ProposalReview,
    WaitlistSignup,
)
from src.services.email_notifications import _generate_unsubscribe_token
from tests import factories

# Tables written by _seed(), truncated (CASCADE) by teardown(). Never
# alembic_version.
_SEEDED_TABLES = (
    "users",
    "agents",
    "researcher_profiles",
    "simulation_runs",
    "agent_channels",
    "agent_messages",
    "llm_call_logs",
    "thread_decisions",
    "proposal_reviews",
    "agent_delegates",
    "delegate_invitations",
    "access_allowlist",
    "waitlist_signups",
    "jobs",
    "cohorts",
    "cohort_memberships",
)


async def _seed(session: AsyncSession) -> dict:
    n = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)

    admin = await factories.make_user(
        session,
        name="Resp Admin",
        orcid=f"0000-0001-{n}-0001",
        email=f"resp-admin-{n}@example.edu",
        is_admin=True,
    )
    admin_agent = await factories.make_agent(
        session,
        user=admin,
        agent_id="respadmin",
        bot_name="RespAdminBot",
        pi_name=admin.name,
        status="active",
    )

    researcher = await factories.make_user(
        session,
        name="Resp Researcher",
        orcid=f"0000-0001-{n}-0002",
        email=f"resp-researcher-{n}@example.edu",
    )
    res_agent = await factories.make_agent(
        session,
        user=researcher,
        agent_id="respres",
        bot_name="RespResBot",
        pi_name=researcher.name,
        status="active",
    )
    await factories.make_profile(session, user=researcher)

    run = await factories.make_simulation_run(
        session, status="completed", started_at=now,
    )
    channel = await factories.make_agent_channel(
        session,
        run=run,
        channel_id="CRESP0001",
        channel_name="general",
        channel_type="thematic",
        created_by_agent="respres",
    )

    root_ts = f"{int(now.timestamp())}.000100"
    reply_ts_1 = f"{int(now.timestamp()) + 1}.000200"
    reply_ts_2 = f"{int(now.timestamp()) + 2}.000300"

    root = await factories.make_agent_message(
        session,
        run=run,
        agent_id="respres",
        channel_id=channel.channel_id,
        channel_name=channel.channel_name,
        message_ts=root_ts,
        thread_ts=None,
        phase="new_post",
        visibility="public",
        content="RespResBot proposes a joint study on responsive rendering.",
        sender_name="RespResBot",
        is_bot=True,
        posted_at=now.timestamp(),
        slack_ts=root_ts,
    )
    await factories.make_agent_message(
        session,
        run=run,
        agent_id="respres",
        channel_id=channel.channel_id,
        channel_name=channel.channel_name,
        message_ts=reply_ts_1,
        thread_ts=root.message_ts,
        phase="thread_reply",
        visibility="public",
        content="Following up with more detail.",
        sender_name="RespResBot",
        is_bot=True,
        posted_at=now.timestamp() + 1,
        slack_ts=reply_ts_1,
    )
    await factories.make_agent_message(
        session,
        run=run,
        agent_id=None,
        channel_id=channel.channel_id,
        channel_name=channel.channel_name,
        message_ts=reply_ts_2,
        thread_ts=root.message_ts,
        phase="thread_reply",
        visibility="public",
        content="Sounds great, let's talk more.",
        sender_name=researcher.name,
        is_bot=False,
        posted_at=now.timestamp() + 2,
        slack_ts=reply_ts_2,
        sender_user_id=researcher.id,
    )

    await factories.make_llm_call_log(
        session,
        run=run,
        agent_id="respres",
        phase="thread_reply",
        channel=channel.channel_name,
        response_text="A discussion turn long enough to pass the length filter.",
        created_at=now - timedelta(minutes=5),
    )

    decision_unreviewed = await factories.make_thread_decision(
        session,
        run=run,
        thread_id=f"{n}.unrev",
        channel=channel.channel_name,
        agent_a="respres",
        agent_b="respadmin",
        outcome="proposal",
        summary_text=":memo: Summary — Joint study on responsive rendering.",
        decided_at=now,
    )
    decision_reviewed = await factories.make_thread_decision(
        session,
        run=run,
        thread_id=f"{n}.rev",
        channel=channel.channel_name,
        agent_a="respres",
        agent_b="respadmin",
        outcome="proposal",
        summary_text=":memo: Summary — A second, already-reviewed proposal.",
        decided_at=now,
    )
    session.add(
        ProposalReview(
            thread_decision_id=decision_reviewed.id,
            agent_id="respres",
            user_id=researcher.id,
            reviewed_by_user_id=researcher.id,
            rating=3,
            comment="Looks good.",
            submitted_via="web",
        )
    )

    invite_token = secrets.token_hex(20)
    invitation = DelegateInvitation(
        agent_registry_id=admin_agent.id,
        invited_by_user_id=admin.id,
        email=researcher.email,
        token=invite_token,
        status="pending",
        expires_at=now + timedelta(days=1),
    )
    session.add(invitation)
    await session.flush()

    session.add(
        AgentDelegate(
            agent_registry_id=admin_agent.id,
            user_id=researcher.id,
            invitation_id=invitation.id,
        )
    )

    await factories.make_user(
        session,
        name="Resp Pending",
        orcid=f"0000-0001-{n}-0003",
        email=f"resp-pending-{n}@example.edu",
        access_status="pending",
        onboarding_complete=False,
    )

    session.add(
        AccessAllowlist(
            orcid=f"0000-0001-{n}-0004",
            note="responsive tier fixture",
            email=f"resp-allowlist-{n}@example.edu",
            added_by_user_id=admin.id,
        )
    )
    session.add(
        WaitlistSignup(
            email=f"resp-waitlist-{n}@example.edu",
            name="Resp Waitlist",
            institution="Test University",
        )
    )

    for status in ("pending", "processing", "completed", "failed", "dead"):
        session.add(
            Job(
                type="generate_profile",
                status=status,
                user_id=researcher.id,
                payload={"user_id": str(researcher.id)},
            )
        )

    cohort = Cohort(name=f"resp-cohort-{n}", description="Responsive tier fixture", created_by=admin.id)
    session.add(cohort)
    await session.flush()
    session.add(
        CohortMembership(cohort_id=cohort.id, agent_id="respres", added_by=admin.id)
    )

    onboarding_user = await factories.make_user(
        session,
        name="Resp Onboarding",
        orcid=f"0000-0001-{n}-0005",
        email=f"resp-onboarding-{n}@example.edu",
        onboarding_complete=False,
    )
    await factories.make_profile(session, user=onboarding_user)
    session.add(
        Job(
            type="generate_profile",
            status="completed",
            user_id=onboarding_user.id,
            payload={"user_id": str(onboarding_user.id)},
        )
    )

    await session.flush()

    return {
        "admin_id": str(admin.id),
        "researcher_id": str(researcher.id),
        "onboarding_id": str(onboarding_user.id),
        "admin_agent": admin_agent.agent_id,
        "res_agent": res_agent.agent_id,
        "run_id": str(run.id),
        "thread_ts": root.message_ts,
        "cohort_id": str(cohort.id),
        "user_detail_id": str(researcher.id),
        "agent_row_id": str(res_agent.id),
        "invite_token": invite_token,
        "unsub_token": _generate_unsubscribe_token(str(researcher.id)),
        "_decision_unreviewed_id": str(decision_unreviewed.id),
    }


_ALLOWED_DB_RE = re.compile(r"^copi_(test|e2e|resp\w*|x\d+|a\d+)$")


def _assert_safe_database(url: str) -> None:
    """Refuse to seed or TRUNCATE anything but a throwaway test database.

    Mirrors tests/e2e/seed.py's allowlist. This module commits and then truncates
    16 tables with CASCADE, so a TEST_DATABASE_URL that names the dev database or
    a production copy must fail here, not at teardown.
    """
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not _ALLOWED_DB_RE.match(name):
        raise RuntimeError(
            f"refusing to seed/truncate database {name!r}: the responsive tier only runs "
            "against copi_test or a copi_x<N>/copi_a<N>/copi_resp* scratch database"
        )


async def run(url: str) -> dict:
    """Seed the database at ``url`` and return the ids dict."""
    _assert_safe_database(url)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            ids = await _seed(session)
            await session.commit()
        finally:
            await session.close()
    finally:
        await engine.dispose()
    return ids


async def teardown(url: str) -> None:
    """Truncate every table :func:`run` wrote to (CASCADE follows FKs into child tables
    such as publications/proposal_votes; every other tier rolls back, so nothing else is
    live). Never touches alembic_version."""
    _assert_safe_database(url)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET statement_timeout = '30s'"))
            await conn.execute(
                text(f"TRUNCATE {', '.join(_SEEDED_TABLES)} CASCADE")
            )
    finally:
        await engine.dispose()
