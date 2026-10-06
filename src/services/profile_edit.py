"""The one profile writer (RB-09): /profile/save, the manager's PI-edit route
(design decision D8), /onboarding/save-profile and
/agent/{id}/public-profile/save all call apply_profile_edits. target_user is whose
profile changes; changed_by_user_id is who made the change — they differ
exactly when a manager edits a PI's profile, and create_revision's existing
changed_by_user_id parameter already supports that attribution without any
schema change."""
import re
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from src.models import AgentRegistry, Job, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.services.grant_sections import load_grant_sections
from src.services.jhu_rules import get_tenure_start, set_tenure_start
from src.services.job_queue import request_job
from src.services.profile_publish import (
    export_and_record,
    lock_persona_writer,
    write_persona_files,
)
from src.services.tenure_scope import scoped_publications_for_export
from src.services.user_email import assign_user_email
from src.services.validators import is_valid_email

#: Text/list fields of ResearcherProfile that the profile forms edit.
PROFILE_FIELDS = (
    "research_summary", "techniques", "experimental_models",
    "disease_areas", "key_targets", "keywords",
)
#: List-valued profile fields. Each tag is posted as its own form field (D-16): the
#: comma split this replaced broke every tag containing a comma ("1,2-dichloroethane").
_LIST_FIELDS = frozenset(PROFILE_FIELDS) - {"research_summary"}
#: users.name / institution / department are String(255) (src/models/user.py).
#: Checked before any write so an overlong value is a form error, not a 500 (A-15).
USER_FIELD_MAX_CHARS = 255
#: Each tag widget (templates/_tag_field.html) posts one marker naming its field, so a
#: widget whose tags were all removed (posting no values) still clears the field, while
#: a form without the widget, or a page rendered before it existed, changes nothing.
TAG_FIELDS_MARKER = "tag_fields"


def list_fields_from_form(form: FormData) -> dict[str, list[str] | None]:
    """The list fields of a posted profile form: the repeated values of each field whose
    widget marker was posted, else None (leave unchanged)."""
    present = set(form.getlist(TAG_FIELDS_MARKER))
    return {
        field: ([v for v in form.getlist(field) if isinstance(v, str)]
                if field in present else None)
        for field in PROFILE_FIELDS if field in _LIST_FIELDS
    }


def _clean_tags(values: Sequence[str]) -> list[str]:
    """Stripped, non-empty tags. A ``str`` is refused: it would iterate as characters,
    and a comma string is exactly the format this replaced."""
    if isinstance(values, str):
        raise TypeError("list fields take a list of tags, not a comma-separated string")
    return [v.strip() for v in values if v.strip()]


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


#: The refusal code for a save that would race a profile generation (D-08); every profile
#: form maps it to "Profile is being generated — try again shortly".
PROFILE_GENERATING = "profile_generating"
#: ``SET LOCAL lock_timeout`` for the profile-row insert (D-08). A generation job holds the
#: uncommitted row for this user for its whole run (the worker keeps one transaction across
#: the pipeline), so without a bound the insert waits on the unique key until the run ends.
PROFILE_INSERT_LOCK_TIMEOUT = "5s"
#: Postgres SQLSTATE ``lock_not_available``: what an expired ``lock_timeout`` raises.
_LOCK_NOT_AVAILABLE = "55P03"


async def _profile_generation_in_flight(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """True while a ``generate_profile`` job for ``user_id`` is pending or processing."""
    return await db.scalar(
        select(Job.id).where(
            Job.user_id == user_id,
            Job.type == "generate_profile",
            Job.status.in_(("pending", "processing")),
        ).limit(1)
    ) is not None


async def _apply_email(
    db: AsyncSession, target_user: User, raw: str | None, email_required: bool,
) -> str | None:
    """Validate, then assign, the posted address; an error code or None. A refusal writes
    nothing: the checks run first, and ``assign_user_email`` writes nothing when another
    user holds the address."""
    email_clean = (raw or "").strip().lower()
    if email_required and not email_clean:
        return "email_required"
    changed = email_clean != (target_user.email or "")
    if email_clean and (changed or email_required) and not is_valid_email(email_clean):
        return "invalid_email"
    if changed and not await assign_user_email(db, target_user, email_clean or None):
        return "email_taken"
    return None


async def _write_tenure_if_changed(
    db: AsyncSession, user: User, year: int, export_agent: AgentRegistry | None,
) -> None:
    """Upsert a ``manual`` tenure entry only when ``year`` differs from the recorded one
    (D-01), and then request an ``enrich_grants`` job in the same transaction. The manager
    form re-posts the displayed year on every save; relabelling a machine-derived entry
    ``manual`` made the re-derivation script skip it."""
    agent_slug = export_agent.agent_id if export_agent is not None else await db.scalar(
        select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user.id)
    )
    if await get_tenure_start(db, user.id, agent_id=agent_slug) != year:
        await set_tenure_start(user.id, year, "manual", db=db)
        # The stored RePORTER rows were fetched under the old fiscal-year filter (spec
        # 2026-10-05 §6.1; §6.3 later moves this to a generate_profile enqueue).
        await request_job(
            db, type="enrich_grants", user_id=user.id,
            payload={"user_id": str(user.id), "orcid": user.orcid},
            priority=INTERACTIVE_PRIORITY,
        )


async def _bounded_persona_lock(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """``lock_persona_writer`` under ``PROFILE_INSERT_LOCK_TIMEOUT``; False (the session
    rolled back) when the wait timed out because a generation job holds the lock."""
    await db.execute(text(f"SET LOCAL lock_timeout = '{PROFILE_INSERT_LOCK_TIMEOUT}'"))
    try:
        await lock_persona_writer(db, user_id)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        await db.rollback()
        return False
    await db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    return True


async def _load_or_create_profile(
    db: AsyncSession, user_id: uuid.UUID,
) -> ResearcherProfile | None:
    """The user's profile row, inserted when missing; None when the insert's lock wait
    timed out (the session is then rolled back)."""
    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )).scalar_one_or_none()
    if profile is not None:
        return profile
    # Before db.add(): an execute autoflushes, and the INSERT must run under the bound.
    await db.execute(text(f"SET LOCAL lock_timeout = '{PROFILE_INSERT_LOCK_TIMEOUT}'"))
    profile = ResearcherProfile(user_id=user_id)
    db.add(profile)
    try:
        # Flush the row into existence before the SQL-side bump in
        # write_profile_text_fields: on a pending object the expression would render
        # inside the INSERT's VALUES, which cannot reference its own target table.
        await db.flush()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        await db.rollback()
        return None
    # The bound is for the INSERT only: the rest of the request's transaction (the
    # field writes and the export) waits as it did before.
    await db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    return profile


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
    form: Mapping[str, str | Sequence[str] | None], expected_version: int | None,
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
    List fields take a list of tags (``list_fields_from_form``).
    ``expected_version`` is the profile-version check (``write_profile_text_fields``);
    None saves without it. ``export_agent`` skips the agent lookup when the caller
    already holds the agent. ``mechanism`` is the revision mechanism recorded
    for the export (``web_impersonated`` for an impersonated session). A name,
    institution or department longer than ``USER_FIELD_MAX_CHARS`` returns
    ``field_too_long`` before anything is written. Returns an error code, or None after
    committing; the persona revision is recorded in a second transaction and the file is
    written after that commit (spec 2026-10-05 §4.3).

    Every refusal comes before any write, because ``get_db`` commits on a clean return: an
    overlong user field (A-15), a ``generate_profile`` job pending or processing for ``target_user`` (``PROFILE_GENERATING``,
    D-08), a malformed tenure year, then the email checks (D-02). The tenure year is written
    only when it differs from the recorded one (D-01), and such a write also requests an
    ``enrich_grants`` job (a refused save writes neither). The profile-row insert runs under
    ``PROFILE_INSERT_LOCK_TIMEOUT``; a lock timeout rolls back and also returns
    ``PROFILE_GENERATING``.
    """
    for field in ("name", "institution", "department"):
        if len(form.get(field) or "") > USER_FIELD_MAX_CHARS:
            return "field_too_long"
    if await _profile_generation_in_flight(db, target_user.id):
        return PROFILE_GENERATING
    # Optional JHU tenure-start correction (manager form only; the PI's own
    # /profile/save never sends the field). Blank = leave unchanged.
    tenure_field = (jhu_tenure_start or "").strip()
    if tenure_field and not re.fullmatch(r"\d{4}", tenure_field):
        return "invalid_tenure_year"
    # Persona writer locks before the first write (the email, user, tenure and profile
    # rows; lock order: profile_publish module docstring), held until the commit below.
    # Bounded like the profile-row insert: a generation job holds the persona lock from
    # tenure derivation through export, so a save that slipped past the in-flight check
    # (the job was claimed just after it) refuses instead of waiting out the whole run.
    if not await _bounded_persona_lock(db, target_user.id):
        return PROFILE_GENERATING
    if form.get("email") is not None:
        email_error = await _apply_email(db, target_user, form["email"], email_required)
        if email_error:
            return email_error
    # Only after every refusal: get_db commits on a clean return, so a tenure upsert made
    # before a refused email used to be committed with the refusal (D-02).
    if tenure_field:
        await _write_tenure_if_changed(db, target_user, int(tenure_field), export_agent)

    if form.get("name"):
        target_user.name = form["name"]
    for field in ("institution", "department"):
        if form.get(field) is not None:
            setattr(target_user, field, form[field] or None)

    profile = await _load_or_create_profile(db, target_user.id)
    if profile is None:
        return PROFILE_GENERATING

    values = {
        f: (_clean_tags(form[f]) if f in _LIST_FIELDS else form[f])
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
    grants = await load_grant_sections(db, target_user.id)
    rendered = await export_and_record(
        db, user=target_user, profile=profile, agent=agent, publications=user_pubs,
        grants=grants, mechanism=mechanism, changed_by_user_id=changed_by_user_id,
        change_summary=change_summary,
    )
    if rendered is not None:
        await db.commit()
        await write_persona_files(db, target_user.id)
    return None
