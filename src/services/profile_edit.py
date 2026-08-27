"""Shared profile-field mutation, used by both the PI's own /profile/save
and the manager's PI-edit route (design decision D8). target_user is whose
profile changes; changed_by_user_id is who made the change — they differ
exactly when a manager edits a PI's profile, and create_revision's existing
changed_by_user_id parameter already supports that attribution without any
schema change."""
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Publication, ResearcherProfile, User
from src.services.jhu_rules import get_tenure_start, set_tenure_start
from src.services.validators import is_valid_email


def _parse_list(val: str) -> list[str]:
    return [s.strip() for s in val.split(",") if s.strip()]


async def apply_profile_edits(
    db: AsyncSession, *, target_user: User, changed_by_user_id: uuid.UUID,
    name: str, email: str, institution: str, department: str,
    research_summary: str, techniques: str, experimental_models: str,
    disease_areas: str, key_targets: str, keywords: str,
    jhu_tenure_start: str | None = None,
) -> str | None:
    # Optional JHU tenure-start correction (manager form only; the PI's own
    # /profile/save never sends the field). Blank = leave unchanged.
    tenure_field = (jhu_tenure_start or "").strip()
    if tenure_field:
        if not re.fullmatch(r"\d{4}", tenure_field):
            return "invalid_tenure_year"
        await set_tenure_start(
            target_user.id, int(tenure_field), "manual", db=db
        )

    email_clean = (email or "").strip().lower()
    if email_clean != (target_user.email or ""):
        if email_clean:
            if not is_valid_email(email_clean):
                return "invalid_email"
            existing = await db.execute(
                select(User).where(
                    User.email == email_clean, User.id != target_user.id
                )
            )
            if existing.scalar_one_or_none():
                return "email_taken"
        target_user.email = email_clean or None

    if name:
        target_user.name = name
    if institution is not None:
        target_user.institution = institution or None
    if department is not None:
        target_user.department = department or None

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == target_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=target_user.id)
        db.add(profile)
        # Flush the row into existence before the SQL-side bump below: on a
        # pending object the expression would render inside the INSERT's
        # VALUES, which cannot reference its own target table.
        await db.flush()

    profile.research_summary = research_summary
    profile.techniques = _parse_list(techniques)
    profile.experimental_models = _parse_list(experimental_models)
    profile.disease_areas = _parse_list(disease_areas)
    profile.key_targets = _parse_list(key_targets)
    profile.keywords = _parse_list(keywords)
    # SQL-side increment (matches profile.py's own fix for issue #22 C1) —
    # nothing below reads profile_version, so the expiry this expression
    # assignment causes needs no refresh here.
    profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1

    await db.commit()

    from src.models import AgentRegistry
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == target_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id_for_export = agent_reg.agent_id if agent_reg else None

    from src.services.profile_export import export_profile_to_markdown
    pub_result = await db.execute(
        select(Publication).where(Publication.user_id == target_user.id)
    )
    user_pubs = list(pub_result.scalars().all())
    # JHU R2's export rule, applied at THIS export site too (audit H3):
    # storage is full-career, and an unfiltered top-20 is exactly how
    # pre-tenure papers reached 9 agents' prompts on 2026-08-14.
    tenure_start_year = await get_tenure_start(
        db, target_user.id, agent_id=agent_id_for_export
    )
    if tenure_start_year is not None:
        user_pubs = [
            p for p in user_pubs if p.year and p.year >= tenure_start_year
        ]
    exported_path = export_profile_to_markdown(
        target_user, profile, agent_id_for_export, publications=user_pubs
    )

    from src.services.profile_versioning import create_revision
    if agent_reg and exported_path:
        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="public",
            content=exported_path.read_text(encoding="utf-8"),
            changed_by_user_id=changed_by_user_id,
            mechanism="web",
        )
        await db.commit()

    return None
