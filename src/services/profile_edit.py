"""Shared profile-field mutation, used by both the PI's own /profile/save
and the manager's PI-edit route (design decision D8). target_user is whose
profile changes; changed_by_user_id is who made the change — they differ
exactly when a manager edits a PI's profile, and create_revision's existing
changed_by_user_id parameter already supports that attribution without any
schema change."""
import re
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ResearcherProfile, User
from src.services.jhu_rules import set_tenure_start
from src.services.user_email import assign_user_email
from src.services.validators import is_valid_email


def _parse_list(val: str) -> list[str]:
    return [s.strip() for s in val.split(",") if s.strip()]


def parse_expected_version(raw: str | None) -> int | None:
    """A form's hidden ``profile_version``, or None when the post carried none
    (a page rendered before the field existed, or a scripted post), which saves
    without the check, as before."""
    text = (raw or "").strip()
    return int(text) if text.isdigit() else None


async def write_profile_text_fields(
    db: AsyncSession, profile: ResearcherProfile, fields: dict, expected_version: int | None,
) -> bool:
    """Write edited profile fields and bump ``profile_version``.

    With ``expected_version`` this is ``UPDATE ... WHERE id = :id AND
    profile_version = :expected``: if a regeneration or another edit bumped the
    version since the form was rendered, nothing is written and this returns
    False; the caller rolls back and asks the user to reload. Without it, the
    ORM write as before. The SQL-side increment is kept either way (the Python
    read-modify-write lost updates when two writers raced). The caller commits.
    """
    if expected_version is None:
        for key, value in fields.items():
            setattr(profile, key, value)
        profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1
        return True
    result = await db.execute(
        update(ResearcherProfile)
        .where(
            ResearcherProfile.id == profile.id,
            ResearcherProfile.profile_version == expected_version,
        )
        .values(**fields, profile_version=func.coalesce(ResearcherProfile.profile_version, 0) + 1)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    await db.refresh(profile)
    return True


async def apply_profile_edits(
    db: AsyncSession, *, target_user: User, changed_by_user_id: uuid.UUID,
    name: str, email: str, institution: str, department: str,
    research_summary: str, techniques: str, experimental_models: str,
    disease_areas: str, key_targets: str, keywords: str,
    jhu_tenure_start: str | None = None,
    expected_profile_version: int | None = None,
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
        if email_clean and not is_valid_email(email_clean):
            return "invalid_email"
        if not await assign_user_email(db, target_user, email_clean or None):
            return "email_taken"

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

    written = await write_profile_text_fields(db, profile, {
        "research_summary": research_summary,
        "techniques": _parse_list(techniques),
        "experimental_models": _parse_list(experimental_models),
        "disease_areas": _parse_list(disease_areas),
        "key_targets": _parse_list(key_targets),
        "keywords": _parse_list(keywords),
    }, expected_profile_version)
    if not written:
        # A regeneration or another edit saved first: keep theirs.
        await db.rollback()
        return "profile_changed"

    await db.commit()

    from src.models import AgentRegistry
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == target_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id_for_export = agent_reg.agent_id if agent_reg else None

    from src.services.profile_export import export_profile_to_markdown
    from src.services.tenure_scope import scoped_publications_for_export
    # JHU R2's export rule, applied at THIS export site too (audit H3):
    # storage is full-career, and an unfiltered top-20 is exactly how
    # pre-tenure papers reached 9 agents' prompts on 2026-08-14.
    user_pubs = await scoped_publications_for_export(
        db, target_user.id, agent_id_for_export
    )
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
