"""ORCID-driven PI onboarding, shared by the manager Add-PI route and admin's
impersonate-if-new path. Ports the fetch->create->enqueue logic that was
duplicated inline in src/routers/admin.py's impersonate_user — see design
decision D7. A third copy remains in src/cli.py's _seed_one_orcid,
deliberately not unified here: that CLI path reuses an existing user and
falls back to a stub user on fetch failure, both of which this function
refuses by design (D6)."""

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import USER_ROLE_PI, AgentRegistry, Job, User
from src.services.agent_identity import derive_agent_identity
from src.services.jhu_rules import derive_employment_start, set_tenure_start
from src.services.orcid import fetch_orcid_profile

logger = logging.getLogger(__name__)

# Format-only (no checksum): every real iD matches, and it is enough to keep
# arbitrary text out of the ORCID URL path, PubMed [auid]/[Author] search
# terms, and the manager page's error redirect (audit L1).
_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


async def find_or_create_pi_by_orcid(db: AsyncSession, orcid: str) -> User:
    """Create a PI User + enqueue a generate_profile Job for one ORCID iD.

    Unlike admin's impersonate flow (which reuses an existing User of any
    role silently), this is an explicit creation action (D6): it raises if
    the ORCID already belongs to anyone, rather than returning their
    existing row. Raises ValueError on either failure mode; never returns
    None. Does not commit — the caller decides the transaction boundary.

    Also persists the employment-derived JHU tenure start when the ORCID
    record carries a current Hopkins employment with a start year — derived
    at add time, where the record is already in hand, so the manager can see
    and correct it on the PI page before the profile job even runs (audit
    H1/H2: the paper-derived fallback lives in the pipeline and only
    persists from a fully resolved corpus).
    """
    orcid = orcid.strip()
    if not _ORCID_RE.match(orcid):
        raise ValueError(f"Invalid ORCID iD format: {orcid[:40]!r}")
    existing = (
        await db.execute(select(User).where(User.orcid == orcid))
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"A user with ORCID {orcid} already exists")

    try:
        profile_data = await fetch_orcid_profile(orcid)
    except Exception as exc:
        raise ValueError(f"Could not fetch ORCID profile for {orcid}: {exc}") from exc

    user = User(
        orcid=orcid,
        name=profile_data.get("name", orcid),
        email=profile_data.get("email"),
        institution=profile_data.get("institution"),
        department=profile_data.get("department"),
        user_role=USER_ROLE_PI,
    )
    db.add(user)
    await db.flush()

    tenure_year = derive_employment_start(profile_data.get("employments") or [])
    if tenure_year is not None:
        await set_tenure_start(user.id, tenure_year, "orcid_employment", db=db)
        logger.info(
            "Tenure start %d (orcid_employment) recorded for %s", tenure_year, orcid
        )

    job = Job(
        type="generate_profile",
        user_id=user.id,
        payload={"user_id": str(user.id), "orcid": orcid},
    )
    db.add(job)
    return user


async def create_pending_agent_for(db: AsyncSession, user: User) -> AgentRegistry:
    """Mint the PI's AgentRegistry row, status='pending' (inert).

    A pending row is a slug reservation plus a queue entry on /admin/agents:
    the engine's roster sync loads only status='active', so the bot cannot
    poll, post, or spend a token until an admin provisions a Slack app and
    explicitly activates it. Idempotent — one lab per user (design D7,
    2026-08-17 account-types design) is enforced by the unique user_id and
    respected here by returning the existing row. Does not commit.
    """
    existing = (
        await db.execute(
            select(AgentRegistry).where(AgentRegistry.user_id == user.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    agent_id, bot_name = await derive_agent_identity(db, user.name, orcid=user.orcid)
    agent = AgentRegistry(
        agent_id=agent_id,
        user_id=user.id,
        bot_name=bot_name,
        pi_name=user.name,
        status="pending",
    )
    db.add(agent)
    await db.flush()
    return agent
