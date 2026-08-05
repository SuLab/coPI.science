"""Seed the browser-flow (e2e) database.

Run *inside* a container whose ``DATABASE_URL`` points at the e2e database
(``copi_slack_test``), which is already at alembic head::

    docker exec -i app-8002 python -m tests.e2e.seed

Idempotent: every row is looked up by its natural key first, so re-running only
tops up what is missing. It commits for real — unlike the rest of the suite this
module writes to a live database on purpose, because the flows it supports are
driven by a browser against a running server, not by an ASGI transport inside a
rolled-back transaction.

**Never point this at the production ``copi`` database.** It refuses any
database name that is not on ``ALLOWED_DATABASES``.

What it creates, and which flow needs it:

===========================  ====================================================
row                          used by
===========================  ====================================================
``ADMIN_ORCID`` user         every ``/admin/**`` flow (forged session cookie)
``SIGNUP_ORCID`` user        agent self-service signup (``POST /agent/request``)
``ONBOARDING_ORCID`` user    the onboarding walk (deliberately no profile/job)
``PROBE_AGENT_ID`` agent     Slack provisioning (``*ProbeBot``, status=pending)
5 Scripps agents + edges     ``/scripps-graph`` and ``/cabo-graph`` render
===========================  ====================================================
"""

import asyncio
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import delete, select

# Identities the browser flows log in as. ORCIDs are in the ISNI test range that
# orcid.org never issues, so these rows can never collide with a real login.
ADMIN_ORCID = "0000-0002-0000-9001"
ADMIN_EMAIL = "e2e-admin@example.org"
SIGNUP_ORCID = "0000-0002-0000-9002"
SIGNUP_EMAIL = "e2e-signup@example.org"
ONBOARDING_ORCID = "0000-0002-0000-9003"
ONBOARDING_EMAIL = "e2e-onboarding@example.org"

# The agent provisioned against real Slack. The name MUST end in "ProbeBot":
# scripts/slack_test_teardown.py deletes apps by that suffix and refuses to
# touch anything else, so a bot named otherwise becomes permanent litter in the
# workspace.
PROBE_AGENT_ID = "t12probe"
PROBE_BOT_NAME = "T12ProbeBot"

# Graph fixture. agent_ids are drawn from src/routers/public.py::_SCRIPPS so the
# scripps_only node filter keeps them; the edge set is a connected component so
# _largest_component() does not trim it.
GRAPH_AGENTS = ["su", "wiseman", "grotjahn", "ward", "briney"]
GRAPH_EDGES = [
    ("su", "wiseman"),
    ("su", "grotjahn"),
    ("wiseman", "ward"),
    ("grotjahn", "briney"),
]
# Inside the Cabo retreat window (Apr 27 - May 7 2026) that /cabo-graph slices
# on, and after CABO_WINDOW_START (Mar 1 2026) which bounds /scripps-graph.
POST_AT = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
DECIDED_AT = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

# Guard rail: the production database is at alembic 0018 and has real users.
ALLOWED_DATABASES = ("copi_slack_test", "copi_test", "copi_e2e")


def _assert_safe_database() -> str:
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name not in ALLOWED_DATABASES:
        sys.exit(
            f"refusing to seed database {name!r}: not in {ALLOWED_DATABASES}. "
            "Set DATABASE_URL to the e2e database."
        )
    return name


async def _get_or_create_user(session, orcid, *, name, email, **kw):
    from src.models import User

    row = (
        await session.execute(select(User).where(User.orcid == orcid))
    ).scalar_one_or_none()
    if row:
        return row, False
    row = User(orcid=orcid, name=name, email=email, **kw)
    session.add(row)
    await session.flush()
    return row, True


async def _get_or_create_agent(session, agent_id, **kw):
    from src.models import AgentRegistry

    row = (
        await session.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
        )
    ).scalar_one_or_none()
    if row:
        return row, False
    row = AgentRegistry(agent_id=agent_id, **kw)
    session.add(row)
    await session.flush()
    return row, True


async def seed(session) -> dict[str, str]:
    """Create every fixture row. Returns a summary keyed by flow."""
    from src.models import (
        AgentChannel,
        AgentMessage,
        AgentRegistry,
        Job,
        ResearcherProfile,
        SimulationRun,
        ThreadDecision,
    )

    out: dict[str, str] = {}

    admin, _ = await _get_or_create_user(
        session,
        ADMIN_ORCID,
        name="E2E Admin",
        email=ADMIN_EMAIL,
        institution="Scripps Research",
        is_admin=True,
        access_status="allowed",
        onboarding_complete=True,
    )
    admin.is_admin = True  # repair a row that predates the flag
    out["admin_user_id"] = str(admin.id)

    # Self-service signup needs a completed profile and no agent of its own.
    # "Quinn Probesmith" -> last name "probesmith" -> agent_id "probesmith";
    # no collision, so the control half of the wu/pwu rule is what we observe.
    signup, created = await _get_or_create_user(
        session,
        SIGNUP_ORCID,
        name="Quinn Probesmith",
        email=SIGNUP_EMAIL,
        institution="Scripps Research",
        access_status="allowed",
        onboarding_complete=True,
    )
    if created:
        session.add(
            ResearcherProfile(
                user_id=signup.id,
                research_summary="Studies chemical probes of protein function.",
                techniques=["mass spectrometry", "chemoproteomics"],
                keywords=["covalent probes", "target ID"],
                private_profile_md="# Private\nE2E fixture.",
                profile_version=1,
            )
        )
    out["signup_user_id"] = str(signup.id)

    # The onboarding walk deliberately gets NO profile and NO job, so
    # /onboarding renders its first ("Building Your Profile") state.
    onboarding, _ = await _get_or_create_user(
        session,
        ONBOARDING_ORCID,
        name="Robin Onboard",
        email=ONBOARDING_EMAIL,
        institution="Scripps Research",
        access_status="allowed",
        onboarding_complete=False,
    )
    # ...and RESET the row if a previous run walked it. Get-or-create alone does
    # not deliver the state the paragraph above promises, because the flow is
    # destructive to its own fixture: its last step POSTs
    # /onboarding/private-profile, which sets onboarding_complete=True, and its
    # "substitute" step leaves a ResearcherProfile and a generate_profile job in
    # status 'completed' behind. Any of the three and the flow is unreplayable —
    # `onboarding_complete` makes /onboarding 302 straight to /profile
    # (src/routers/onboarding.py::onboarding_start, first statement), and a
    # 'completed' job takes profile_review.html past the spinner branch. Measured
    # on copi_slack_test 2026-08-04: all three were set from the 2026-07-31 run,
    # so the flow had silently stopped testing anything a browser would see.
    # Scoped to this one fixture ORCID; nothing else is deleted anywhere here.
    onboarding.onboarding_complete = False
    await session.execute(
        delete(ResearcherProfile).where(ResearcherProfile.user_id == onboarding.id)
    )
    await session.execute(
        delete(Job).where(Job.user_id == onboarding.id, Job.type == "generate_profile")
    )
    out["onboarding_user_id"] = str(onboarding.id)

    probe, _ = await _get_or_create_agent(
        session,
        PROBE_AGENT_ID,
        bot_name=PROBE_BOT_NAME,
        pi_name="T12 Probe",
        status="pending",
    )
    out["probe_agent_row_id"] = str(probe.id)
    out["probe_agent_id"] = probe.agent_id

    # --- graph fixture --------------------------------------------------
    run = (
        await session.execute(select(SimulationRun).limit(1))
    ).scalar_one_or_none()
    if run is None:
        run = SimulationRun(status="completed", config={"seed": "tests.e2e.seed"})
        session.add(run)
        await session.flush()
    out["simulation_run_id"] = str(run.id)

    channel = (
        await session.execute(
            select(AgentChannel).where(AgentChannel.channel_name == "e2e-general")
        )
    ).scalar_one_or_none()
    if channel is None:
        channel = AgentChannel(
            simulation_run_id=run.id,
            channel_id="C0E2E0001",
            channel_name="e2e-general",
            channel_type="thematic",
            created_by_agent=GRAPH_AGENTS[0],
        )
        session.add(channel)
        await session.flush()

    for i, agent_id in enumerate(GRAPH_AGENTS):
        user, _ = await _get_or_create_user(
            session,
            f"0000-0002-0000-91{i:02d}",
            name=f"PI {agent_id.title()}",
            email=f"e2e-{agent_id}@example.org",
            institution="Scripps Research",
            access_status="allowed",
            onboarding_complete=True,
        )
        agent = (
            await session.execute(
                select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
            )
        ).scalar_one_or_none()
        if agent is None:
            session.add(
                AgentRegistry(
                    agent_id=agent_id,
                    user_id=user.id,
                    bot_name=f"{agent_id.title()}Bot",
                    pi_name=f"PI {agent_id.title()}",
                    status="active",
                )
            )
    await session.flush()

    for i, (a, b) in enumerate(GRAPH_EDGES):
        ts = f"17{i:08d}.000100"
        existing = (
            await session.execute(
                select(AgentMessage).where(
                    AgentMessage.simulation_run_id == run.id,
                    AgentMessage.message_ts == ts,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                AgentMessage(
                    simulation_run_id=run.id,
                    agent_id=a,
                    channel_id=channel.channel_id,
                    channel_name=channel.channel_name,
                    message_ts=ts,
                    phase="new_post",
                    visibility="public",
                    content=f"{a} proposes work with {b}.",
                    sender_name=f"{a.title()}Bot",
                    message_length=40,
                    posted_at=POST_AT.timestamp(),
                    created_at=POST_AT,
                )
            )
        decided = (
            await session.execute(
                select(ThreadDecision).where(ThreadDecision.thread_id == ts)
            )
        ).scalar_one_or_none()
        if decided is None:
            session.add(
                ThreadDecision(
                    simulation_run_id=run.id,
                    thread_id=ts,
                    channel=channel.channel_name,
                    agent_a=a,
                    agent_b=b,
                    outcome="proposal",
                    origin_visibility="public",
                    summary_text=(
                        f"{a.title()} and {b.title()} propose a joint study "
                        "combining their platforms."
                    ),
                    decided_at=DECIDED_AT,
                )
            )
    await session.commit()
    out["graph_edges"] = str(len(GRAPH_EDGES))
    return out


async def main() -> None:
    name = _assert_safe_database()
    from src.database import get_session_factory

    async with get_session_factory()() as session:
        summary = await seed(session)
    print(f"seeded {name}:")
    for k, v in summary.items():
        print(f"  {k} = {v}")


if __name__ == "__main__":
    asyncio.run(main())
