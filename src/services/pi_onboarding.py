"""ORCID-driven PI onboarding, shared by the manager Add-PI route and admin's
impersonate-if-new path. Ports the fetch->create->enqueue logic that used to
be duplicated in src/cli.py's _seed_one_orcid and inline in
src/routers/admin.py's impersonate_user — see design decision D7."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import USER_ROLE_PI, Job, User
from src.services.orcid import fetch_orcid_profile


async def find_or_create_pi_by_orcid(db: AsyncSession, orcid: str) -> User:
    """Create a PI User + enqueue a generate_profile Job for one ORCID iD.

    Unlike admin's impersonate flow (which reuses an existing User of any
    role silently), this is an explicit creation action (D6): it raises if
    the ORCID already belongs to anyone, rather than returning their
    existing row. Raises ValueError on either failure mode; never returns
    None. Does not commit — the caller decides the transaction boundary.
    """
    orcid = orcid.strip()
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

    job = Job(
        type="generate_profile",
        user_id=user.id,
        payload={"user_id": str(user.id), "orcid": orcid},
    )
    db.add(job)
    return user
