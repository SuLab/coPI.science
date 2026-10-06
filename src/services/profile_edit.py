"""The one writer of human profile edits (RB-09): /profile/save, the manager's PI-edit
route (design decision D8), /onboarding/save-profile and /agent/{id}/public-profile/save
all call apply_profile_edits. (The profile pipeline and staff actions on the manager PI
page write profile fields too.) target_user is whose profile changes; changed_by_user_id
is who made the change — they differ exactly when a manager edits a PI's profile, and
create_revision's existing changed_by_user_id parameter already supports that attribution
without any schema change.

A save that changes a profile text field stamps ``human_edited_at`` (spec 2026-10-05
§6.3, D19): a later regeneration then stages a draft instead of overwriting the edit. A
tenure-year change requests a regeneration (``generate_profile``), whose step 10 refreshes
grants and enrichment."""
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from src.models import AgentRegistry, Job, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.services.grant_sections import load_grant_sections
from src.services.jhu_rules import get_tenure_start, set_tenure_start
from src.services.job_queue import request_job
from src.services.person_names import InvalidPersonName, validate_person_name
from src.services.profile_limits import (
    SUMMARY_MAX_CHARS,
    SUMMARY_MAX_WORDS,
    TAG_LIST_MAX_ITEMS,
    TAG_MAX_CHARS,
)
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
    (a page rendered before the field existed, or a scripted post): ``apply_profile_edits``
    then refuses a save over an existing profile (``profile_version_missing``)."""
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
#: A save over an existing profile without the form's profile_version (spec 2026-10-05
#: §6.4, P2): a form rendered before the profile existed would otherwise blank it.
PROFILE_VERSION_MISSING = "profile_version_missing"
#: A changed name outside the D60 allowlist (person_names.validate_person_name).
INVALID_NAME = "invalid_name"
#: D24 caps on a changed value (src/services/profile_limits.py).
SUMMARY_TOO_LONG = "summary_too_long"
TOO_MANY_TAGS = "too_many_tags"
TAG_TOO_LONG = "tag_too_long"
TAG_INVALID = "tag_invalid"
#: A first save that would create an empty profile (D-15, every save path).
NOTHING_TO_SAVE = "nothing_to_save"
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
    (D-01), and then request a ``generate_profile`` job in the same transaction (spec
    2026-10-05 §6.3, D20: its step 10 re-fetches grants under the new fiscal-year filter
    and refreshes enrichment). The manager form re-posts the displayed year on every save;
    relabelling a machine-derived entry ``manual`` made the re-derivation script skip it."""
    agent_slug = export_agent.agent_id if export_agent is not None else await db.scalar(
        select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user.id)
    )
    if await get_tenure_start(db, user.id, agent_id=agent_slug) != year:
        await set_tenure_start(user.id, year, "manual", db=db)
        # The new year changes the synthesis scope, the export scope and the RePORTER
        # fiscal-year filter (spec 2026-10-05 §6.3, D20).
        await request_job(
            db, type="generate_profile", user_id=user.id,
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


async def _existing_profile(
    db: AsyncSession, user_id: uuid.UUID,
) -> ResearcherProfile | None:
    """The user's profile row as committed now (an identity-map copy is refreshed), or None.
    Called under the persona lock, so a generation that committed meanwhile is visible."""
    return (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


def _text_field_changed(profile: ResearcherProfile | None, field: str, value: object) -> bool:
    """Whether writing ``value`` (as apply_profile_edits writes it) changes ``field``. None
    and [] (a list field) and None and "" (the summary) are the same value."""
    empty: object = [] if field in _LIST_FIELDS else ""
    stored = getattr(profile, field) if profile is not None else None
    return (stored or empty) != (value or empty)


def _edit_cap_error(
    profile: ResearcherProfile | None, values: Mapping[str, object],
) -> str | None:
    """The D24 refusal code for ``values`` (spec 2026-10-05 §6.4), or None. Only changes are
    checked, so text saved before the caps existed can be re-posted unchanged: the summary
    when it differs from the stored one, a tag not already in that stored list, and a list
    longer than both the cap and the stored list."""
    summary = values.get("research_summary")
    if (
        isinstance(summary, str)
        and _text_field_changed(profile, "research_summary", summary)
        and (len(summary) > SUMMARY_MAX_CHARS or len(summary.split()) > SUMMARY_MAX_WORDS)
    ):
        return SUMMARY_TOO_LONG
    for field in sorted(_LIST_FIELDS & values.keys()):
        tags = values[field]
        stored = list(getattr(profile, field) or []) if profile is not None else []
        if len(tags) > TAG_LIST_MAX_ITEMS and len(tags) > len(stored):
            return TOO_MANY_TAGS
        known = set(stored)
        for tag in tags:
            if tag in known:
                continue
            if len(tag) > TAG_MAX_CHARS:
                return TAG_TOO_LONG
            if "\n" in tag or "\r" in tag or tag.startswith("#"):
                return TAG_INVALID
    return None


def _has_profile_content(values: Mapping[str, object]) -> bool:
    """A non-blank summary or at least one tag among the posted profile fields."""
    return any(
        (bool(v) if isinstance(v, list) else bool((v or "").strip()))
        for v in values.values()
    )


def _carries_user_fields(form: Mapping[str, object], tenure_field: str) -> bool:
    """The form posts a user field (name, email, institution, department) or a tenure year."""
    return bool(tenure_field) or any(
        form.get(f) is not None for f in ("name", "email", "institution", "department")
    )


def _check_user_fields(
    target_user: User, form: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """``(error code, validated new name)`` for the posted user fields. A name,
    institution or department over ``USER_FIELD_MAX_CHARS`` is ``field_too_long``; a
    non-blank name must pass ``validate_person_name`` (D60; an unchanged name passes
    unchecked), else ``INVALID_NAME``. The name is None when the form posts a blank one."""
    for field in ("name", "institution", "department"):
        if len(form.get(field) or "") > USER_FIELD_MAX_CHARS:
            return "field_too_long", None
    if not (form.get("name") or "").strip():
        return None, None
    try:
        return None, validate_person_name(form["name"], previous=target_user.name)
    except InvalidPersonName:
        return INVALID_NAME, None


async def _write_user_fields(
    db: AsyncSession, target_user: User, form: Mapping[str, object], new_name: str | None,
) -> None:
    """Write the posted name (copied to ``agents.pi_name`` when it changed), institution and
    department; a field the form does not carry is left alone."""
    if new_name is not None and new_name != target_user.name:
        target_user.name = new_name
        await _sync_agent_pi_name(db, target_user.id, new_name)
    for field in ("institution", "department"):
        if form.get(field) is not None:
            setattr(target_user, field, form[field] or None)


async def _sync_agent_pi_name(db: AsyncSession, user_id: uuid.UUID, name: str) -> None:
    """``agents.pi_name`` follows a name edit (spec 2026-10-05 §6.4, D23): headlines and the
    lab prompt read it, and ``_sync_roster_from_db`` refreshes a running engine's copy."""
    await db.execute(
        update(AgentRegistry).where(AgentRegistry.user_id == user_id).values(pi_name=name)
    )


async def _is_identical_resubmission(
    db: AsyncSession, target_user: User, profile: ResearcherProfile,
    form: Mapping[str, object], values: Mapping[str, object], tenure_field: str,
    new_name: str | None,
) -> bool:
    """Whether everything the form carries already equals what is stored: the same edit was
    saved a moment ago, so a double submit that lost the version check is reported as
    saved (spec 2026-10-05 D48). ``profile`` was read under the persona lock; the user row
    is re-read here. Compares only profile text fields, the user fields the form carries
    and the tenure year. Reads only."""
    user = (await db.execute(
        select(User).where(User.id == target_user.id).execution_options(populate_existing=True)
    )).scalar_one()
    if any(_text_field_changed(profile, f, v) for f, v in values.items() if f in PROFILE_FIELDS):
        return False
    if new_name is not None and user.name != new_name:
        return False
    for field in ("institution", "department"):
        if form.get(field) is not None and (form[field] or None) != getattr(user, field):
            return False
    if form.get("email") is not None and (
        str(form["email"] or "").strip().lower() != (user.email or "")
    ):
        return False
    if tenure_field:
        slug = await db.scalar(
            select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user.id)
        )
        if await get_tenure_start(db, user.id, agent_id=slug) != int(tenure_field):
            return False
    return True


async def write_profile_text_fields(
    db: AsyncSession, profile: ResearcherProfile, fields: dict, expected_version: int | None,
) -> bool:
    """Write edited profile fields and bump ``profile_version``.

    With ``expected_version`` this is ``UPDATE ... WHERE id = :id AND
    profile_version = :expected``: if a regeneration or another edit bumped the
    version since the form was rendered, nothing is written and this returns
    False; the caller rolls back and asks the user to reload. Without it, the
    ORM write as before. The SQL-side increment is kept either way (the Python
    read-modify-write lost updates when two writers raced). ``fields`` may include
    ``human_edited_at``. The caller commits.
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

    A key the form does not carry (absent or None) leaves that field unchanged: the
    public-profile and onboarding forms have no institution field, and every route posts
    ``research_summary`` as ``Form(None)``. A key carried as "" clears a user field
    (``or None``) or the summary. List fields take a list of tags
    (``list_fields_from_form``). A blank name leaves ``users.name`` unchanged; a changed
    name must pass ``person_names.validate_person_name`` (D60) and is copied to
    ``agents.pi_name`` (D23). ``export_agent`` skips the agent lookup when the caller
    already holds the agent. ``mechanism`` is the revision mechanism of the export
    (``web_impersonated`` under impersonation).

    Returns an error code, or None after committing. Every refusal comes before any write,
    because ``get_db`` commits on a clean return: a user field over
    ``USER_FIELD_MAX_CHARS`` (``field_too_long``), an invalid changed name
    (``invalid_name``), a ``generate_profile`` job pending or processing
    (``PROFILE_GENERATING``, D-08), a malformed tenure year, the persona lock not granted
    within ``PROFILE_INSERT_LOCK_TIMEOUT`` (``PROFILE_GENERATING``), an existing profile
    without ``expected_version`` (``profile_version_missing``, spec 2026-10-05 §6.4), a
    version that moved since the form was rendered (``profile_changed``, unless everything
    posted already equals what is stored: then None and nothing is written, D48), a D24 cap
    on a changed value (``summary_too_long``, ``too_many_tags``, ``tag_too_long``,
    ``tag_invalid``), a first save with nothing to put in the profile and no user field
    (``nothing_to_save``), then the email checks (D-02). The tenure year is written only
    when it differs from the recorded one (D-01), and such a write also requests a
    ``generate_profile`` job. A save that changes a text field stamps ``human_edited_at``.
    With no profile and no profile content the user fields are saved and no profile is
    created. The persona revision is recorded in the edit's transaction under its
    existing persona lock; the file is written after that commit (§4.3). Whether it succeeded is not
    returned (``profile_publish.persona_out_of_date`` reads it back).
    """
    user_field_error, new_name = _check_user_fields(target_user, form)
    if user_field_error:
        return user_field_error
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
    profile = await _existing_profile(db, target_user.id)
    if profile is not None and expected_version is None:
        # A form rendered before this profile existed posts no version; saving it would
        # blank the generated profile (spec 2026-10-05 §6.4, P2).
        return PROFILE_VERSION_MISSING
    values = {
        f: (_clean_tags(form[f]) if f in _LIST_FIELDS else form[f])
        for f in PROFILE_FIELDS if form.get(f) is not None
    }
    if profile is not None and profile.profile_version != expected_version:
        # Every profile writer holds the persona lock taken above, so the version cannot
        # move again before this transaction ends; the conditional UPDATE below stays as
        # the backstop. A double submit of the edit just saved is reported as saved (D48).
        if await _is_identical_resubmission(
            db, target_user, profile, form, values, tenure_field, new_name
        ):
            return None
        return "profile_changed"
    cap_error = _edit_cap_error(profile, values)
    if cap_error:
        return cap_error
    writes_profile = profile is not None or _has_profile_content(values)
    if not writes_profile and not _carries_user_fields(form, tenure_field):
        return NOTHING_TO_SAVE
    if form.get("email") is not None:
        email_error = await _apply_email(db, target_user, form["email"], email_required)
        if email_error:
            return email_error
    # Only after every refusal: get_db commits on a clean return, so a tenure upsert made
    # before a refused email used to be committed with the refusal (D-02).
    if tenure_field:
        await _write_tenure_if_changed(db, target_user, int(tenure_field), export_agent)

    await _write_user_fields(db, target_user, form, new_name)
    if not writes_profile:
        # No profile and nothing to put in one: the user fields are saved and no empty
        # profile is minted (D-15, every save path); there is no persona to export.
        await db.commit()
        return None

    if profile is None:
        profile = await _load_or_create_profile(db, target_user.id)
        if profile is None:
            return PROFILE_GENERATING
    fields = dict(values)
    if any(_text_field_changed(profile, f, v) for f, v in values.items()):
        # Only a real change of a text field is a human edit (D19, spec §6.3): the forms
        # re-post every field, and a tenure-only save must not hold back regenerations.
        # An identical resubmission returned above, so it never stamps.
        fields["human_edited_at"] = datetime.now(UTC)
    if not await write_profile_text_fields(db, profile, fields, expected_version):
        # A writer that does not take the persona lock saved first: keep theirs.
        await db.rollback()
        return "profile_changed"

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
    # Keep the snapshot and revision atomic with the edit. Committing first
    # would release the lock before these render inputs were loaded/recorded.
    await db.commit()
    if rendered is not None:
        await write_persona_files(db, target_user.id)
    return None
