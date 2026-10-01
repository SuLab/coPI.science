"""The one profile writer (RB-09): /profile/save, the manager's PI-edit route
(design decision D8), /onboarding/save-profile and
/agent/{id}/public-profile/save all call apply_profile_edits. target_user is whose
profile changes; changed_by_user_id is who made the change — they differ
exactly when a manager edits a PI's profile, and create_revision's existing
changed_by_user_id parameter already supports that attribution without any
schema change."""
import re
import uuid
from collections.abc import Mapping

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, ResearcherProfile, User
from src.services.jhu_rules import set_tenure_start
from src.services.profile_publish import export_and_record
from src.services.tenure_scope import scoped_publications_for_export
from src.services.user_email import assign_user_email
from src.services.validators import is_valid_email

#: Text/list fields of ResearcherProfile that the profile forms edit.
PROFILE_FIELDS = (
    "research_summary", "techniques", "experimental_models",
    "disease_areas", "key_targets", "keywords",
)
#: List-valued profile fields (comma-separated in the forms).
_LIST_FIELDS = frozenset(PROFILE_FIELDS) - {"research_summary"}


def _parse_list(val: str) -> list[str]:
    return [s.strip() for s in val.split(",") if s.strip()]


def parse_expected_version(raw: str | None) -> int | None:
    """A form's hidden ``profile_version``, or None when the post carried none
    (a page rendered before the field existed, or a scripted post), which saves
    without the check, as before."""
    text = (raw or "").strip()
    # ASCII digits only, and within the Integer column: ``str.isdigit`` also
    # accepts characters ``int()`` rejects (e.g. "²"), and a larger number
    # cannot be bound against the column.
    if not re.fullmatch(r"[0-9]{1,9}", text):
        return None
    return int(text)


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
    form: Mapping[str, str | None], expected_version: int | None,
    jhu_tenure_start: str | None = None, email_required: bool = False,
    change_summary: str | None = None, export_agent: AgentRegistry | None = None,
    mechanism: str = "web",
) -> str | None:
    """The one writer behind /profile/save, /agent/{id}/public-profile/save,
    /onboarding/save-profile and /manager/pis/{id}/profile (RB-09).

    A key the form does not carry (absent or None) leaves that field unchanged:
    the public-profile and onboarding forms have no institution field, and
    blanking it would drop the export's header lines (FA3-V5). A key carried as
    "" clears a user field (``or None``) exactly as the full forms always did.
    ``expected_version`` is the profile-version check (``write_profile_text_fields``);
    None saves without it. ``export_agent`` skips the agent lookup when the caller
    already holds the agent. ``mechanism`` is the revision mechanism recorded
    for the export (``web_impersonated`` for an impersonated session). Returns an
    error code, or None after committing and exporting.
    """
    # Optional JHU tenure-start correction (manager form only; the PI's own
    # /profile/save never sends the field). Blank = leave unchanged.
    tenure_field = (jhu_tenure_start or "").strip()
    if tenure_field:
        if not re.fullmatch(r"\d{4}", tenure_field):
            return "invalid_tenure_year"
        await set_tenure_start(
            target_user.id, int(tenure_field), "manual", db=db
        )

    if form.get("email") is not None:
        email_clean = (form["email"] or "").strip().lower()
        if email_required and not email_clean:
            return "email_required"
        changed = email_clean != (target_user.email or "")
        if email_clean and (changed or email_required) and not is_valid_email(email_clean):
            return "invalid_email"
        if changed and not await assign_user_email(db, target_user, email_clean or None):
            return "email_taken"

    if form.get("name"):
        target_user.name = form["name"]
    for field in ("institution", "department"):
        if form.get(field) is not None:
            setattr(target_user, field, form[field] or None)

    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == target_user.id)
    )).scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=target_user.id)
        db.add(profile)
        # Flush the row into existence before the SQL-side bump below: on a
        # pending object the expression would render inside the INSERT's
        # VALUES, which cannot reference its own target table.
        await db.flush()

    values = {
        f: (_parse_list(form[f]) if f in _LIST_FIELDS else form[f])
        for f in PROFILE_FIELDS if form.get(f) is not None
    }
    if not await write_profile_text_fields(db, profile, values, expected_version):
        # A regeneration or another edit saved first: keep theirs.
        await db.rollback()
        return "profile_changed"

    await db.commit()

    agent = export_agent or (await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == target_user.id)
    )).scalar_one_or_none()
    # JHU R2's export rule, applied at THIS export site too (audit H3):
    # storage is full-career, and an unfiltered top-20 is exactly how
    # pre-tenure papers reached 9 agents' prompts on 2026-08-14.
    user_pubs = await scoped_publications_for_export(
        db, target_user.id, agent.agent_id if agent else None
    )
    path = await export_and_record(
        db, user=target_user, profile=profile, agent=agent, publications=user_pubs,
        mechanism=mechanism, changed_by_user_id=changed_by_user_id,
        change_summary=change_summary,
    )
    if path is not None and agent is not None:
        await db.commit()
    return None
