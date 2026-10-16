"""ORCID-driven PI onboarding for the manager Add-PI route. Ports the
fetch->create->enqueue logic that was once duplicated inline in
src/routers/admin/impersonation.py's impersonate_user — see design decision D7;
impersonation now only looks an account up and never creates one (A-16). `cli seed-profile` shares `validate_orcid` and
`record_employment_tenure` with this module (MD-14) but keeps its own
reuse-existing-user behaviour, which `find_or_create_pi_by_orcid` refuses by
design (D6). Neither creates a user named by its ORCID iD (spec 2026-10-05 §6.3):
Add-PI asks for the name, the CLI refuses."""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import USER_ROLE_PI, AgentRegistry, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.services.agent_identity import derive_agent_identity
from src.services.identifiers import normalize_orcid
from src.services.jhu_rules import derive_employment_start, get_tenure_start, set_tenure_start
from src.services.orcid import fetch_orcid_profile
from src.services.person_names import (
    is_orcid_like,
    name_from_machine_source,
    validate_person_name,
)
from src.services.profile_jobs import enqueue_profile_job_if_absent
from src.services.user_email import assign_user_email

logger = logging.getLogger(__name__)

# Q21 (2026-10-15 publication-handling design): an iD must pass ORCID's ISO 7064
# MOD 11-2 check digit, so a mistyped iD is refused before any ORCID fetch, and only a
# well-formed iD reaches the ORCID URL path, PubMed [auid]/[Author] search terms and
# the manager page's error redirect.
def validate_orcid(orcid: str) -> str:
    """The canonical iD (``identifiers.normalize_orcid``; an orcid.org URL, a lowercase
    ``x``, hyphenless and single-spaced forms are accepted), shared by Add-PI, the Staff
    form and `cli seed-profile` (MD-14). Raises ``ValueError`` containing "Invalid ORCID"
    (``_pi_common._create_pi_error_code`` maps it to ``invalid_orcid``) when the text is
    not an iD or its check digit does not match."""
    normalized = normalize_orcid(orcid)
    if normalized is None:
        raise ValueError(f"Invalid ORCID iD: {orcid.strip()[:40]!r}")
    return normalized


async def record_employment_tenure(db: AsyncSession, user: User, profile_data: dict) -> int | None:
    """Persist the employment-derived JHU tenure start when ORCID carries one."""
    tenure_year = derive_employment_start(profile_data.get("employments") or [])
    if tenure_year is not None:
        await set_tenure_start(user.id, tenure_year, "orcid_employment", db=db)
        logger.info("Tenure start %d (orcid_employment) recorded for %s", tenure_year, user.orcid)
    return tenure_year


class OrcidNameRequired(ValueError):
    """Add-PI for an ORCID record with no public name and no name entered (spec §6.3)."""

    def __init__(self, orcid: str) -> None:
        super().__init__(f"ORCID record {orcid} has no public name; enter the PI's name")
        self.orcid = orcid


async def find_or_create_pi_by_orcid(
    db: AsyncSession, orcid: str, *, name: str | None = None,
) -> User:
    """Create a PI User + enqueue its generate_profile job (src/services/profile_jobs.py) for one ORCID iD.

    This is an explicit creation action (D6): it raises if
    the ORCID already belongs to anyone, rather than returning their
    existing row. Raises ValueError on either failure mode; never returns
    None. Does not commit — the caller decides the transaction boundary.

    Also persists the employment-derived JHU tenure start when the ORCID
    record carries a current Hopkins employment with a start year — derived
    at add time, where the record is already in hand, so the manager can see
    and correct it on the PI page before the profile job even runs (audit
    H1/H2: the paper-derived fallback lives in the pipeline and only
    persists from a fully resolved corpus).

    ``name`` is the manager's entry for a record with no public name: validated with
    ``person_names.validate_person_name`` (``InvalidPersonName`` propagates) and used
    only when ORCID has none; without it such a record raises ``OrcidNameRequired``
    (spec 2026-10-05 §6.3: never the iD). When ORCID has a name, ``name`` is ignored.
    """
    orcid = validate_orcid(orcid)
    existing = (
        await db.execute(select(User).where(User.orcid == orcid))
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"A user with ORCID {orcid} already exists")

    try:
        profile_data = await fetch_orcid_profile(orcid)
    except Exception as exc:
        raise ValueError(f"Could not fetch ORCID profile for {orcid}: {exc}") from exc

    # An ORCID-sourced name is cut to the D60 allowlist; a cut is stamped for the
    # manager PI page. A typed name is validated, never cut, so nothing is stamped.
    if profile_data.get("name"):
        name, cut = name_from_machine_source(profile_data["name"])
    elif name is not None and name.strip():
        name, cut = validate_person_name(name), False
    else:
        raise OrcidNameRequired(orcid)
    user = User(
        orcid=orcid,
        name=name,
        name_sanitized_at=datetime.now(UTC) if cut else None,
        institution=profile_data.get("institution"),
        department=profile_data.get("department"),
        user_role=USER_ROLE_PI,
    )
    db.add(user)
    await db.flush()
    if profile_data.get("email"):
        await assign_user_email(db, user, profile_data["email"])

    await record_employment_tenure(db, user, profile_data)

    await enqueue_profile_job_if_absent(db, user, priority=INTERACTIVE_PRIORITY)
    return user


async def adopt_agentless_pi(
    db: AsyncSession, orcid: str, *, name: str | None = None,
) -> User | None:
    """The PI account that already holds ``orcid`` but has no lab agent, readied for
    the Add-PI flow; None when no account holds the iD, including one deleted during
    the ORCID fetch (the caller then creates the account as for a new PI).

    A PI who signed in with ORCID before anyone added them has a ``pi`` User (pending
    access until an admin approves it) and no AgentRegistry row — only Add-PI mints
    one for a manager — so "already exists" used to leave the manager with no way to
    give that PI a lab. Such an account is adopted: its profile job is enqueued when
    it has no profile yet (the login enqueues none for a pending account), and the
    employment-derived tenure start is recorded when none is set, best effort. The
    caller then mints the agent exactly as for a new PI.

    Raises ValueError ("already exists") for any other holder of the iD: a staff
    account, a denied account, or a PI who already has an agent (D6). Validates the
    iD like ``find_or_create_pi_by_orcid``. Does not commit.

    A validated ``name`` (``person_names.validate_person_name``; ``InvalidPersonName``
    propagates) replaces the adopted account's name only when that name is empty, an
    ORCID iD or letter-less (``person_names.is_orcid_like``).

    The holder is checked twice: unlocked before the ORCID fetch, and again under
    ``FOR UPDATE`` after it, before anything is written, so a staff conversion or a
    newly minted agent that commits during the fetch is refused rather than
    overwritten (staff-accounts plan §5.5); a holder gone by then returns None with
    nothing written. No row lock is held across the fetch,
    which can take up to 3 x 30 s. The row lock is held until the caller's
    transaction ends.
    """
    orcid = validate_orcid(orcid)
    user = (
        await db.execute(select(User).where(User.orcid == orcid))
    ).scalar_one_or_none()
    if user is None:
        return None
    await _require_agentless_pi(db, user, orcid)

    profile_data = None
    if await get_tenure_start(db, user.id) is None:
        try:
            profile_data = await fetch_orcid_profile(orcid)
        except Exception as exc:  # the pipeline derives a paper-based start instead
            logger.warning("Adopting %s: ORCID record unavailable (%s)", orcid, exc)

    # populate_existing: a plain select would hand back the identity-map object with
    # the values read before the fetch.
    user = (
        await db.execute(
            select(User).where(User.id == user.id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        return None  # deleted during the fetch; the caller's create handles the iD
    await _require_agentless_pi(db, user, orcid)

    if name is not None and name.strip() and (
        not (user.name or "").strip() or is_orcid_like(user.name)
    ):
        user.name = validate_person_name(name)

    if profile_data is not None and await get_tenure_start(db, user.id) is None:
        await record_employment_tenure(db, user, profile_data)
    has_profile = await db.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
    )
    if has_profile is None:
        await enqueue_profile_job_if_absent(db, user, priority=INTERACTIVE_PRIORITY)
    return user


async def _require_agentless_pi(db: AsyncSession, user: User, orcid: str) -> None:
    """Raise the D6 "already exists" ValueError unless ``user`` is a non-denied
    ``pi`` with no AgentRegistry row."""
    has_agent = await db.scalar(
        select(AgentRegistry.id).where(AgentRegistry.user_id == user.id)
    )
    if user.user_role != USER_ROLE_PI or user.access_status == "denied" or has_agent:
        raise ValueError(f"A user with ORCID {orcid} already exists")


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
    # pi_name is users.name as stored. A machine-sourced name was already sanitised (and
    # name_sanitized_at stamped) where it entered users.name; a human-typed one is not
    # rewritten here, so nothing is stamped (the flag means users.name was cut).
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
