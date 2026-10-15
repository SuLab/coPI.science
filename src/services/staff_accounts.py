"""Manager and reviewer accounts with no lab bot (plan
docs/plans/2026-10-13-staff-accounts-without-a-bot-plan.md).

The one place the Staff page's rules live: who may create which role (D1), which existing
accounts may be converted (D2), and the reviewer revoke/restore/delete actions (D5, D6).
Two manager limits keep an admin-only power out of a manager's reach: a manager may not
create or convert an ORCID iD on the access allowlist (deleting that account would strip the
admin's pre-approval), and may revoke only a reviewer who has signed in (``last_login_at``
set; revoking anyone else would be the admin-only deny). Admins have neither limit.
HTTP-free; nothing here commits except ``delete_reviewer`` (``delete_user_account``
commits). Every action adds an ``admin_audit_events`` row to the session so it commits
with its effect (D3).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AccessAllowlist,
    AdminAuditEvent,
    AgentDelegate,
    AgentRegistry,
    Job,
    ResearcherProfile,
    User,
)
from src.models.job import PER_USER_JOB_TYPES
from src.services.orcid import fetch_orcid_profile
from src.services.person_names import (
    InvalidPersonName,
    is_orcid_like,
    name_from_machine_source,
    validate_person_name,
)
from src.services.pi_onboarding import validate_orcid
from src.services.session_epoch import bump_session_epoch
from src.services.user_deletion import DeletionReport, delete_user_account, research_data_for
from src.services.user_email import assign_user_email
from src.services.user_roles import RoleChangeRefused, change_user_role

logger = logging.getLogger(__name__)

#: The roles the Staff page can create or convert to. Never ``admin`` (D1).
STAFF_ACCOUNT_ROLES: tuple[str, ...] = (USER_ROLE_MANAGER, USER_ROLE_REVIEWER)

#: Session key holding the add form's last values after a refusal, so the redirect URL
#: carries nothing but the path (``{"orcid", "user_role", "name"}``, validated values only).
STAFF_REFILL_SESSION_KEY = "staff_form_refill"

#: ``admin_audit_events.action`` values written here.
AUDIT_CREATE = "staff_account_create"
AUDIT_CONVERT = "staff_account_convert"
AUDIT_REVOKE = "reviewer_revoke"
AUDIT_RESTORE = "reviewer_restore"
AUDIT_DELETE = "reviewer_delete"
AUDIT_ACTIONS: tuple[str, ...] = (
    AUDIT_CREATE, AUDIT_CONVERT, AUDIT_REVOKE, AUDIT_RESTORE, AUDIT_DELETE,
)

#: Refusal code -> the message the route flashes. Every ``StaffAccountRefused.code`` is a key.
REFUSAL_MESSAGES: dict[str, str] = {
    "invalid_role": "Choose a role you may create.",
    "invalid_orcid": "That doesn't look like an ORCID iD (expected 0000-0000-0000-0000).",
    "fetch_failed": "Could not fetch that ORCID record. Check the iD and try again.",
    "name_required": "This ORCID record shows no public name. Enter the person's name and try again.",
    "invalid_name": "Names may contain letters, spaces, hyphens, apostrophes, periods and commas (at most 100 characters).",
    "exists": "That ORCID iD already belongs to an account that cannot be converted here.",
    "ask_admin": "That ORCID iD already belongs to an account. Ask an admin to change it.",
    "use_account_page": "That ORCID iD already belongs to an account. Change it on its account page.",
    "jobs_running": "That account has profile or enrichment work running. Try again when it finishes.",
    "changed": "Something changed since the preview. Review it again before confirming.",
    "not_found": "That account no longer exists.",
    "not_reviewer": "Only reviewer accounts can be changed here.",
    "self": "You cannot change your own account here.",
    "never_signed_in": "This reviewer has never signed in. Delete the unused account instead, or ask an admin.",
    "not_allowed": "That reviewer's access is not active.",
    "not_denied": "That reviewer's access is not revoked.",
    "has_research_data": "This account holds research data (a researcher profile, publications or a lab agent). Ask an admin to delete it.",
    "create_failed": "Could not complete that. See the server log.",
}


class StaffAccountRefused(ValueError):
    """A refused Staff page action; nothing was written by the service.

    ``code`` is a ``REFUSAL_MESSAGES`` key. ``user_id`` names the existing account for
    ``use_account_page`` (the route links ``/admin/users/{user_id}``). ``preview`` is the
    fresh preview for ``changed``, which the route renders instead of acting."""

    def __init__(
        self, code: str, *, user_id: uuid.UUID | None = None,
        preview: StaffPreview | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.user_id = user_id
        self.preview = preview


@dataclass(frozen=True)
class StaffPreview:
    """What a confirm will do, as the first submit shows it (O2).

    ``create``: ``name``/``institution``/``employments`` come from ORCID's public record
    (``employments`` items are ``{"organization", "start_year", "current"}``);
    ``user_id`` is None, ``access_status`` None, ``has_profile`` False.
    ``convert``: the existing account's stored ``name``/``institution``, ``user_id``,
    ``access_status`` and whether it has a ``researcher_profiles`` row; ``employments`` empty.
    """

    outcome: str  # "create" | "convert"
    orcid: str
    role: str
    name: str
    institution: str | None
    employments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    user_id: uuid.UUID | None = None
    access_status: str | None = None
    has_profile: bool = False

    def expectation(self) -> str:
        """A digest of every fact shown (outcome, orcid, role, user_id, name, institution,
        employments, access_status, has_profile): the confirm form carries it, and a confirm
        whose fresh preview digests differently is refused with ``changed``. Employments are
        digested in their stored order as ``[organization, start_year, current]``."""
        facts = {
            "outcome": self.outcome,
            "orcid": self.orcid,
            "role": self.role,
            "user_id": str(self.user_id) if self.user_id is not None else None,
            "name": self.name,
            "institution": self.institution,
            "employments": [
                [e.get("organization"), e.get("start_year"), e.get("current")]
                for e in self.employments
            ],
            "access_status": self.access_status,
            "has_profile": self.has_profile,
        }
        canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_view(self) -> dict[str, Any]:
        """Plain JSON-native dict for the template (key ``staff_preview``): every field above
        (``user_id`` as str or None, ``employments`` as a list) plus ``expectation``."""
        return {
            "outcome": self.outcome,
            "orcid": self.orcid,
            "role": self.role,
            "name": self.name,
            "institution": self.institution,
            "employments": [dict(e) for e in self.employments],
            "user_id": str(self.user_id) if self.user_id is not None else None,
            "access_status": self.access_status,
            "has_profile": self.has_profile,
            "expectation": self.expectation(),
        }


def creatable_roles(actor: User) -> tuple[str, ...]:
    """Admin -> (manager, reviewer); manager -> (reviewer,); anyone else -> ()."""
    if actor.user_role == USER_ROLE_ADMIN:
        return (USER_ROLE_MANAGER, USER_ROLE_REVIEWER)
    if actor.user_role == USER_ROLE_MANAGER:
        return (USER_ROLE_REVIEWER,)
    return ()


@dataclass
class _Resolved:
    """A passed check run: the preview plus what the write needs and the preview omits."""

    preview: StaffPreview
    holder: User | None = None  # the account to convert; None for a create
    name_cut: bool = False  # the ORCID name was cut to the allowlist (stamps name_sanitized_at)
    department: str | None = None
    email: str | None = None
    replace_name: bool = False  # convert: the stored name is unusable; preview.name replaces it


def _employments(raw: Any) -> tuple[dict[str, Any], ...]:
    items = raw if isinstance(raw, list) else []
    return tuple(
        {
            "organization": e.get("organization"),
            "start_year": e.get("start_year"),
            "current": bool(e.get("current")),
        }
        for e in items if isinstance(e, dict)
    )


def _typed_name(name: str | None) -> str:
    """The validated typed name; ``name_required`` when blank, ``invalid_name`` when invalid."""
    if name is None or not name.strip():
        raise StaffAccountRefused("name_required")
    try:
        return validate_person_name(name)
    except InvalidPersonName:
        raise StaffAccountRefused("invalid_name") from None


async def _lock_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """``user_id``'s row ``FOR UPDATE``. ``populate_existing`` matters: without it a row
    already in the identity map comes back with its stale attribute values, so the checks
    that follow would run on what the session last saw, not on the row just locked."""
    return (
        await db.execute(
            select(User).where(User.id == user_id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _resolve(
    db: AsyncSession, *, actor: User, orcid: str, role: str, name: str | None, lock: bool,
) -> _Resolved:
    """The §5.1 precedence shared by ``preview`` (``lock=False``) and ``create_or_convert``
    (``lock=True``); the first failing check raises. A manager is refused ``ask_admin`` for an
    allowlisted iD before any lookup or ORCID fetch. With ``lock``, a holder is re-read
    ``FOR UPDATE`` before its checks, and a create's ORCID fetch runs with no lock held."""
    if role not in creatable_roles(actor):
        raise StaffAccountRefused("invalid_role")
    try:
        orcid = validate_orcid(orcid or "")
    except ValueError:
        raise StaffAccountRefused("invalid_orcid") from None
    if actor.user_role == USER_ROLE_MANAGER and await db.scalar(
        select(AccessAllowlist.id).where(AccessAllowlist.orcid == orcid).limit(1)
    ) is not None:
        # Deleting the account later removes the allowlist row (an admin's pre-approval).
        raise StaffAccountRefused("ask_admin")

    # populate_existing: the preview must show the row as stored, not a stale session copy.
    holder = (
        await db.execute(
            select(User).where(User.orcid == orcid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if holder is not None and lock:
        holder = await _lock_user(db, holder.id)

    if holder is None:
        try:
            profile = await fetch_orcid_profile(orcid)
        except Exception as exc:
            logger.warning("Staff account %s: ORCID fetch failed (%s)", orcid, exc)
            raise StaffAccountRefused("fetch_failed") from None
        machine, cut = name_from_machine_source(profile.get("name"))
        # An iD-shaped or empty machine name is no name: never name an account by its iD.
        if machine and not is_orcid_like(machine):
            account_name = machine
        else:
            account_name, cut = _typed_name(name), False
        return _Resolved(
            preview=StaffPreview(
                outcome="create",
                orcid=orcid,
                role=role,
                name=account_name,
                institution=profile.get("institution"),
                employments=_employments(profile.get("employments")),
            ),
            name_cut=cut,
            department=profile.get("department"),
            email=profile.get("email"),
        )

    if holder.user_role != USER_ROLE_PI or holder.access_status == "denied":
        raise StaffAccountRefused("exists")
    owns_agent = await db.scalar(
        select(AgentRegistry.id).where(AgentRegistry.user_id == holder.id).limit(1)
    )
    is_delegate = await db.scalar(
        select(AgentDelegate.id).where(AgentDelegate.user_id == holder.id).limit(1)
    )
    if owns_agent is not None or is_delegate is not None:
        if actor.user_role == USER_ROLE_ADMIN:
            raise StaffAccountRefused("use_account_page", user_id=holder.id)
        raise StaffAccountRefused("ask_admin")
    job_running = await db.scalar(
        select(Job.id).where(
            Job.user_id == holder.id,
            Job.type.in_(PER_USER_JOB_TYPES),
            Job.status.in_(("pending", "processing")),
        ).limit(1)
    )
    if job_running is not None:
        raise StaffAccountRefused("jobs_running")
    has_profile = await db.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == holder.id).limit(1)
    ) is not None
    if actor.user_role == USER_ROLE_MANAGER and (
        holder.access_status != "pending" or has_profile
    ):
        raise StaffAccountRefused("ask_admin")
    replace_name = not (holder.name or "").strip() or is_orcid_like(holder.name)
    account_name = _typed_name(name) if replace_name else holder.name
    return _Resolved(
        preview=StaffPreview(
            outcome="convert",
            orcid=orcid,
            role=role,
            name=account_name,
            institution=holder.institution,
            user_id=holder.id,
            access_status=holder.access_status,
            has_profile=has_profile,
        ),
        holder=holder,
        replace_name=replace_name,
    )


def _audit(action: str, actor: User, user: User) -> AdminAuditEvent:
    """The §5.4 row; payload values are JSON-native (the JSONB column cannot hold a UUID)."""
    return AdminAuditEvent(
        action=action,
        actor_user_id=actor.id,
        payload={
            "user_id": str(user.id),
            "orcid": user.orcid,
            "name": user.name,
            "role": user.user_role,
        },
    )


async def preview(
    db: AsyncSession, *, actor: User, orcid: str, role: str, name: str | None = None,
) -> StaffPreview:
    """What ``create_or_convert`` would do, without writing or locking. Raises the same
    ``StaffAccountRefused`` the confirm would raise (§5.1 precedence)."""
    resolved = await _resolve(db, actor=actor, orcid=orcid, role=role, name=name, lock=False)
    return resolved.preview


async def create_or_convert(
    db: AsyncSession, *, actor: User, orcid: str, role: str, name: str | None = None,
    expectation: str,
) -> tuple[User, bool]:
    """Create a new staff account, or convert an existing ``pi`` account (D2); returns
    ``(user, converted)``. Re-runs every check, re-fetches ORCID for a create (before any
    lock), compares the fresh preview's ``expectation()`` with ``expectation`` before any
    write (``changed`` carries the fresh preview), and adds the audit row. Does not commit."""
    resolved = await _resolve(db, actor=actor, orcid=orcid, role=role, name=name, lock=True)
    fresh = resolved.preview
    if fresh.expectation() != expectation:
        raise StaffAccountRefused("changed", preview=fresh)

    if resolved.holder is None:
        user = User(
            orcid=fresh.orcid,
            name=fresh.name,
            name_sanitized_at=datetime.now(UTC) if resolved.name_cut else None,
            institution=fresh.institution,
            department=resolved.department,
            user_role=role,
            access_status="allowed",
        )
        # No lock guards a create (there is no row yet); the unique orcid does. A racing
        # insert of the same iD fails here and only this savepoint rolls back.
        try:
            async with db.begin_nested():
                db.add(user)
                await db.flush()
        except IntegrityError:
            raise StaffAccountRefused("exists") from None
        if resolved.email:
            await assign_user_email(db, user, resolved.email)
        converted = False
    else:
        user = resolved.holder
        try:
            await change_user_role(db, user, role)
        except RoleChangeRefused:
            # Unreachable while the user row is locked (an agent insert needs the row's key
            # share), but refuse like an agent seen by the checks rather than fail.
            if actor.user_role == USER_ROLE_ADMIN:
                raise StaffAccountRefused("use_account_page", user_id=user.id) from None
            raise StaffAccountRefused("ask_admin") from None
        user.access_status = "allowed"
        if resolved.replace_name:
            user.name = fresh.name
        converted = True

    db.add(_audit(AUDIT_CONVERT if converted else AUDIT_CREATE, actor, user))
    logger.info(
        "Staff account %s: %s %s (%s) as %s by %s",
        "converted" if converted else "created", user.id, user.orcid, user.name, role, actor.id,
    )
    return user, converted


def _is_self(actor: User, target: User) -> bool:
    """The actor's own account, or, under impersonation, the real admin's (as
    ``admin_deny_access`` checks)."""
    real_admin = getattr(actor, "_real_admin", None)
    return target.id == actor.id or (real_admin is not None and target.id == real_admin.id)


async def _locked_reviewer(db: AsyncSession, actor: User, user_id: uuid.UUID) -> User:
    """The target loaded once, ``FOR UPDATE`` + ``populate_existing``, past the shared
    refusals in order: ``not_found``, ``not_reviewer``, ``self``. The caller must already be
    staff (the routes' ``get_staff_user``); this does not check the actor's role."""
    target = await _lock_user(db, user_id)
    if target is None:
        raise StaffAccountRefused("not_found")
    if target.user_role != USER_ROLE_REVIEWER:
        raise StaffAccountRefused("not_reviewer")
    if _is_self(actor, target):
        raise StaffAccountRefused("self")
    return target


def _may_revoke_unseen(actor: User) -> bool:
    """Whether ``actor`` may revoke a reviewer who has never signed in: admins only."""
    return actor.user_role == USER_ROLE_ADMIN


async def revoke_reviewer(db: AsyncSession, *, actor: User, user_id: uuid.UUID) -> User:
    """Deny a reviewer's access and bump their session epoch (signs them out everywhere),
    as ``admin_deny_access`` does; adds the audit row. Does not commit.

    A manager (not an admin) may revoke only a reviewer who has signed in
    (``last_login_at`` set; a pending sign-in never sets it): otherwise ``never_signed_in``,
    checked after the shared refusals and before ``not_allowed``."""
    target = await _locked_reviewer(db, actor, user_id)
    if not _may_revoke_unseen(actor) and target.last_login_at is None:
        raise StaffAccountRefused("never_signed_in")
    if target.access_status != "allowed":
        raise StaffAccountRefused("not_allowed")
    target.access_status = "denied"
    await bump_session_epoch(db, target.id)
    db.add(_audit(AUDIT_REVOKE, actor, target))
    logger.info("Reviewer %s (%s) revoked by %s", target.id, target.orcid, actor.id)
    return target


async def restore_reviewer(db: AsyncSession, *, actor: User, user_id: uuid.UUID) -> User:
    """Set a denied reviewer back to ``allowed`` (D6; any denied reviewer); queues nothing;
    adds the audit row. Does not commit."""
    target = await _locked_reviewer(db, actor, user_id)
    if target.access_status != "denied":
        raise StaffAccountRefused("not_denied")
    # The epoch is not touched: the revoke's bump already ended every old session.
    target.access_status = "allowed"
    db.add(_audit(AUDIT_RESTORE, actor, target))
    logger.info("Reviewer %s (%s) restored by %s", target.id, target.orcid, actor.id)
    return target


async def delete_reviewer(
    db: AsyncSession, *, actor: User, user_id: uuid.UUID,
) -> DeletionReport:
    """Delete a reviewer account through ``delete_user_account(remove_from_allowlist=True)``;
    the audit row is added first so both commit in its in-transaction phase. Commits.
    A non-admin actor is refused with ``has_research_data`` when the account holds research
    data (``research_data_for``); nothing is written."""
    target = await _locked_reviewer(db, actor, user_id)
    # Q5: a manager may not destroy research data. The target's users row is locked
    # FOR UPDATE above, which conflicts with the FOR KEY SHARE an FK check takes, so no
    # profile, publication or agent row can be added to it before this decision.
    if actor.user_role != USER_ROLE_ADMIN and (
        await research_data_for(db, [target.id])
    )[target.id].has_any:
        logger.info(
            "Staff delete of %s refused for %s: it holds research data", target.id, actor.id,
        )
        raise StaffAccountRefused("has_research_data")
    # Added before the call: delete_user_account commits inside, so a row added after it
    # would land in a separate transaction and could outlive (or miss) the delete.
    db.add(_audit(AUDIT_DELETE, actor, target))
    target_id, target_orcid = target.id, target.orcid
    report = await delete_user_account(db, target, remove_from_allowlist=True)
    logger.info("Reviewer %s (%s) deleted by %s", target_id, target_orcid, actor.id)
    return report


async def list_staff_accounts(db: AsyncSession, *, viewer: User) -> list[dict[str, Any]]:
    """Every manager, reviewer and admin, by name: ``{"user", "is_self", "has_agent",
    "has_profile", "publication_count", "has_research_data", "delete_needs_admin",
    "can_revoke", "can_restore", "can_delete"}``. The ``can_*`` flags say which reviewer
    actions ``viewer`` may take on the row. ``can_revoke`` is False for a manager viewer on
    a never-signed-in reviewer. ``delete_needs_admin`` is True on a reviewer row holding
    research data when the viewer is not an admin, and ``can_delete`` is then False."""
    users = (
        await db.execute(
            select(User)
            .where(User.user_role.in_((USER_ROLE_MANAGER, USER_ROLE_REVIEWER, USER_ROLE_ADMIN)))
            .order_by(User.name, User.id)
        )
    ).scalars().all()
    data = await research_data_for(db, [u.id for u in users])
    rows: list[dict[str, Any]] = []
    for user in users:
        is_self = _is_self(viewer, user)
        actionable = viewer.is_staff and user.user_role == USER_ROLE_REVIEWER and not is_self
        held = data[user.id]
        delete_needs_admin = (
            actionable and held.has_any and viewer.user_role != USER_ROLE_ADMIN
        )
        rows.append({
            "user": user,
            "is_self": user.id == viewer.id,
            "has_agent": held.agent_id is not None,
            "has_profile": held.has_profile,
            "publication_count": held.publication_count,
            "has_research_data": held.has_any,
            "delete_needs_admin": delete_needs_admin,
            "can_revoke": actionable and user.access_status == "allowed" and (
                user.last_login_at is not None or _may_revoke_unseen(viewer)
            ),
            "can_restore": actionable and user.access_status == "denied",
            "can_delete": actionable and not delete_needs_admin,
        })
    return rows


async def recent_staff_changes(db: AsyncSession, *, limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``AUDIT_ACTIONS`` rows: ``{"action", "actor_name", "target_name",
    "target_orcid", "role", "created_at"}`` (``actor_name`` None when the actor is gone)."""
    result = await db.execute(
        select(AdminAuditEvent, User.name)
        .outerjoin(User, User.id == AdminAuditEvent.actor_user_id)
        .where(AdminAuditEvent.action.in_(AUDIT_ACTIONS))
        .order_by(AdminAuditEvent.created_at.desc(), AdminAuditEvent.id.desc())
        .limit(limit)
    )
    changes: list[dict[str, Any]] = []
    for event, actor_name in result.all():
        payload = event.payload or {}
        changes.append({
            "action": event.action,
            "actor_name": actor_name,
            "target_name": payload.get("name"),
            "target_orcid": payload.get("orcid"),
            "role": payload.get("role"),
            "created_at": event.created_at,
        })
    return changes
