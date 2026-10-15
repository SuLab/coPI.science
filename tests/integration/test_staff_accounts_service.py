"""src/services/staff_accounts.py: creating and converting staff accounts, and the reviewer
revoke/restore/delete actions (plan docs/plans/2026-10-13-staff-accounts-without-a-bot-plan.md
§5.1, §5.4, §6)."""
import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
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
    AppSetting,
    AssessmentChatTurn,
    AssessmentReview,
    AssessmentReviewAssignment,
    Job,
    OpportunityAssessment,
    Publication,
    ResearcherProfile,
    User,
)
from src.models.job import PER_USER_JOB_TYPES
from src.services import staff_accounts
from src.services.jhu_rules import TENURE_KEY_PREFIX
from src.services.staff_accounts import (
    AUDIT_CONVERT,
    AUDIT_CREATE,
    AUDIT_DELETE,
    AUDIT_RESTORE,
    AUDIT_REVOKE,
    REFUSAL_MESSAGES,
    StaffAccountRefused,
    StaffPreview,
)
from src.services.user_deletion import ResearchData, research_data_for
from tests import factories

pytestmark = pytest.mark.integration

_oids = itertools.count(1)


def _new_oid() -> str:
    return f"0000-0003-0000-{next(_oids):04d}"


def _record(oid: str, **overrides) -> dict:
    data = {
        "orcid": oid,
        "name": "Jane Doe",
        "institution": "Johns Hopkins University",
        "department": "Pathology",
        "email": f"staff-{oid}@example.org",
        "employments": [
            {"organization": "Johns Hopkins University", "start_year": 2015, "current": True},
        ],
    }
    data.update(overrides)
    return data


@pytest.fixture
def orcid_records(monkeypatch):
    """Per-iD fake ORCID records: a dict value, an exception to raise, or an async callable.
    An iD with no entry gets ``_record``'s default."""
    records: dict[str, object] = {}
    calls: list[str] = []

    async def fetch(oid):
        calls.append(oid)
        rec = records.get(oid)
        if rec is None:
            return _record(oid)
        if isinstance(rec, Exception):
            raise rec
        if callable(rec):
            return await rec(oid)
        return rec

    monkeypatch.setattr(staff_accounts, "fetch_orcid_profile", fetch)
    records["_calls"] = calls  # type: ignore[assignment]
    return records


async def _refused(coro) -> StaffAccountRefused:
    with pytest.raises(StaffAccountRefused) as exc:
        await coro
    assert exc.value.code in REFUSAL_MESSAGES
    return exc.value


async def _audits(db, action: str | None = None) -> list[AdminAuditEvent]:
    stmt = select(AdminAuditEvent).execution_options(populate_existing=True)
    if action is not None:
        stmt = stmt.where(AdminAuditEvent.action == action)
    return list((await db.execute(stmt)).scalars().all())


async def _users_with_orcid(db, oid: str) -> int:
    return await db.scalar(select(func.count()).select_from(User).where(User.orcid == oid))


async def _stored(db, user_id, *cols):
    return (await db.execute(select(*cols).where(User.id == user_id))).one()


async def _write_elsewhere(db_session, user_id, **values) -> None:
    """Change a users row through a second session on the test's connection, leaving
    ``db_session``'s identity-map copy stale (as another request's commit would)."""
    conn = await db_session.connection()
    other = AsyncSession(bind=conn, expire_on_commit=False,
                         join_transaction_mode="create_savepoint")
    try:
        await other.execute(update(User).where(User.id == user_id).values(**values))
        await other.commit()
    finally:
        await other.close()


def _tree(root) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.fixture
def persona_dir(monkeypatch, tmp_path):
    """``profile_export.PROFILES_DIR`` pointed at a tmp dir holding one sentinel file."""
    from src.services import profile_export

    root = tmp_path / "profiles-public"
    root.mkdir()
    (root / "existing.md").write_text("x")
    monkeypatch.setattr(profile_export, "PROFILES_DIR", root)
    return root


async def _confirm(db, *, actor, orcid, role, name=None):
    """Preview then confirm with the preview's own expectation."""
    shown = await staff_accounts.preview(db, actor=actor, orcid=orcid, role=role, name=name)
    return shown, await staff_accounts.create_or_convert(
        db, actor=actor, orcid=orcid, role=role, name=name, expectation=shown.expectation(),
    )


# ---- creatable_roles, StaffPreview ----------------------------------------------------


@pytest.mark.parametrize("role,expected", [
    (USER_ROLE_ADMIN, (USER_ROLE_MANAGER, USER_ROLE_REVIEWER)),
    (USER_ROLE_MANAGER, (USER_ROLE_REVIEWER,)),
    (USER_ROLE_REVIEWER, ()),
    (USER_ROLE_PI, ()),
])
def test_creatable_roles(role, expected):
    assert staff_accounts.creatable_roles(User(user_role=role)) == expected


def test_expectation_digests_every_shown_fact_and_as_view_is_json_native():
    base = StaffPreview(outcome="convert", orcid="0000-0003-0000-9999", role=USER_ROLE_REVIEWER,
                        name="Jane Doe", institution="JHU", user_id=uuid.uuid4(),
                        access_status="pending", has_profile=False)
    variants = [
        StaffPreview(**{**base.__dict__, "outcome": "create"}),
        StaffPreview(**{**base.__dict__, "orcid": "0000-0003-0000-9998"}),
        StaffPreview(**{**base.__dict__, "role": USER_ROLE_MANAGER}),
        StaffPreview(**{**base.__dict__, "user_id": uuid.uuid4()}),
        StaffPreview(**{**base.__dict__, "name": "Janet Doe"}),
        StaffPreview(**{**base.__dict__, "institution": None}),
        StaffPreview(**{**base.__dict__, "access_status": "allowed"}),
        StaffPreview(**{**base.__dict__, "has_profile": True}),
        StaffPreview(**{**base.__dict__, "employments": (
            {"organization": "JHU", "start_year": 2015, "current": True},)}),
    ]
    digests = {base.expectation()} | {v.expectation() for v in variants}
    assert len(digests) == len(variants) + 1
    assert StaffPreview(**base.__dict__).expectation() == base.expectation()

    view = base.as_view()
    assert view["user_id"] == str(base.user_id)
    assert view["employments"] == []
    assert view["expectation"] == base.expectation()
    assert set(view) == {"outcome", "orcid", "role", "name", "institution", "employments",
                         "user_id", "access_status", "has_profile", "expectation"}


# ---- create -----------------------------------------------------------------------------


@pytest.mark.parametrize("actor_role,role", [
    (USER_ROLE_ADMIN, USER_ROLE_MANAGER),
    (USER_ROLE_ADMIN, USER_ROLE_REVIEWER),
    (USER_ROLE_MANAGER, USER_ROLE_REVIEWER),
])
async def test_create_makes_an_allowed_account_with_nothing_else(
    db_session, orcid_records, monkeypatch, persona_dir, actor_role, role,
):
    tree_before = _tree(persona_dir)
    actor = await factories.make_user(db_session, user_role=actor_role)
    oid = _new_oid()
    assigned: list[str] = []
    real_assign = staff_accounts.assign_user_email

    async def spy(db, user, email):
        assigned.append(email)
        return await real_assign(db, user, email)

    monkeypatch.setattr(staff_accounts, "assign_user_email", spy)

    shown = await staff_accounts.preview(db_session, actor=actor, orcid=oid, role=role)
    assert shown.outcome == "create" and shown.user_id is None and shown.access_status is None
    assert shown.name == "Jane Doe" and shown.institution == "Johns Hopkins University"
    assert shown.employments == (
        {"organization": "Johns Hopkins University", "start_year": 2015, "current": True},
    )
    assert await _users_with_orcid(db_session, oid) == 0  # the preview wrote nothing

    user, converted = await staff_accounts.create_or_convert(
        db_session, actor=actor, orcid=oid, role=role, expectation=shown.expectation(),
    )
    assert converted is False
    row = await _stored(db_session, user.id, User.user_role, User.access_status, User.name,
                        User.institution, User.department, User.email)
    assert tuple(row) == (role, "allowed", "Jane Doe", "Johns Hopkins University",
                          "Pathology", f"staff-{oid}@example.org")
    assert assigned == [f"staff-{oid}@example.org"]
    assert await db_session.scalar(
        select(func.count()).select_from(AgentRegistry).where(AgentRegistry.user_id == user.id)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(Job).where(Job.user_id == user.id)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AppSetting)
        .where(AppSetting.key == f"{TENURE_KEY_PREFIX}{user.id}")
    ) == 0
    assert _tree(persona_dir) == tree_before  # no persona file
    (event,) = await _audits(db_session, AUDIT_CREATE)
    assert event.actor_user_id == actor.id
    assert event.payload == {"user_id": str(user.id), "orcid": oid, "name": "Jane Doe",
                             "role": role}


@pytest.mark.parametrize("actor_role,role", [
    (USER_ROLE_MANAGER, USER_ROLE_MANAGER),
    (USER_ROLE_MANAGER, USER_ROLE_ADMIN),
    (USER_ROLE_MANAGER, USER_ROLE_PI),
    (USER_ROLE_ADMIN, USER_ROLE_ADMIN),
    (USER_ROLE_ADMIN, USER_ROLE_PI),
    (USER_ROLE_ADMIN, ""),
    (USER_ROLE_REVIEWER, USER_ROLE_REVIEWER),
    (USER_ROLE_PI, USER_ROLE_REVIEWER),
])
async def test_a_role_the_actor_may_not_create_is_refused_first(
    db_session, orcid_records, actor_role, role,
):
    actor = await factories.make_user(db_session, user_role=actor_role)
    # An invalid iD too: invalid_role comes first in the precedence.
    for call in (staff_accounts.preview, staff_accounts.create_or_convert):
        kwargs = {"expectation": "x"} if call is staff_accounts.create_or_convert else {}
        exc = await _refused(call(db_session, actor=actor, orcid="nope", role=role, **kwargs))
        assert exc.code == "invalid_role"
    assert orcid_records["_calls"] == []
    assert await _audits(db_session) == []


async def test_a_bad_orcid_is_refused(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    exc = await _refused(staff_accounts.preview(
        db_session, actor=admin, orcid="0000-0000-0000-00", role=USER_ROLE_REVIEWER))
    assert exc.code == "invalid_orcid"
    assert orcid_records["_calls"] == []


async def test_manager_may_not_create_an_allowlisted_orcid(db_session, orcid_records):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    oid = _new_oid()
    db_session.add(AccessAllowlist(orcid=oid))
    await db_session.flush()
    exc = await _refused(staff_accounts.preview(
        db_session, actor=mgr, orcid=oid, role=USER_ROLE_REVIEWER))
    assert exc.code == "ask_admin"
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=mgr, orcid=oid, role=USER_ROLE_REVIEWER, expectation="x"))
    assert exc.code == "ask_admin"
    assert orcid_records["_calls"] == []
    assert await _users_with_orcid(db_session, oid) == 0
    assert await _audits(db_session) == []

    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    _shown, (user, converted) = await _confirm(db_session, actor=admin, orcid=oid,
                                               role=USER_ROLE_REVIEWER)
    assert converted is False and user.orcid == oid
    assert orcid_records["_calls"] == [oid, oid]


async def test_manager_may_not_convert_an_allowlisted_orcid(db_session, orcid_records):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    db_session.add(AccessAllowlist(orcid=pi.orcid))
    await db_session.flush()
    exc = await _refused(staff_accounts.preview(
        db_session, actor=mgr, orcid=pi.orcid, role=USER_ROLE_REVIEWER))
    assert exc.code == "ask_admin"
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=mgr, orcid=pi.orcid, role=USER_ROLE_REVIEWER, expectation="x"))
    assert exc.code == "ask_admin"
    assert (await _stored(db_session, pi.id, User.user_role)).user_role == USER_ROLE_PI
    assert await _audits(db_session) == []


async def test_a_fetch_failure_is_refused(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    orcid_records[oid] = RuntimeError("ORCID is down")
    for call in (staff_accounts.preview, staff_accounts.create_or_convert):
        kwargs = {"expectation": "x"} if call is staff_accounts.create_or_convert else {}
        exc = await _refused(call(db_session, actor=admin, orcid=oid,
                                  role=USER_ROLE_REVIEWER, **kwargs))
        assert exc.code == "fetch_failed"
    assert await _users_with_orcid(db_session, oid) == 0


async def test_a_nameless_record_needs_a_valid_typed_name(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    orcid_records[oid] = _record(oid, name=None)

    exc = await _refused(staff_accounts.preview(
        db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER))
    assert exc.code == "name_required"
    exc = await _refused(staff_accounts.preview(
        db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER, name="Ada <b>"))
    assert exc.code == "invalid_name"

    _shown, (user, _) = await _confirm(db_session, actor=admin, orcid=oid,
                                       role=USER_ROLE_REVIEWER, name="Ada Lovelace")
    assert (await _stored(db_session, user.id, User.name)).name == "Ada Lovelace"


async def test_an_orcid_record_name_wins_over_a_typed_one(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=oid,
                                         role=USER_ROLE_REVIEWER, name="Someone Else")
    assert shown.name == "Jane Doe"


async def test_a_concurrent_create_of_the_same_orcid_is_refused_as_exists(
    db_session, orcid_records,
):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=oid,
                                         role=USER_ROLE_REVIEWER)

    async def racing_fetch(fetched_oid):
        # Another request inserts the iD while this one waits on ORCID.
        conn = await db_session.connection()
        other = AsyncSession(bind=conn, expire_on_commit=False,
                             join_transaction_mode="create_savepoint")
        try:
            await factories.make_user(other, orcid=fetched_oid, access_status="pending")
            await other.commit()
        finally:
            await other.close()
        return _record(fetched_oid)

    orcid_records[oid] = racing_fetch
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER,
        expectation=shown.expectation()))
    assert exc.code == "exists"
    assert await _users_with_orcid(db_session, oid) == 1
    assert await _audits(db_session) == []


# ---- convert ----------------------------------------------------------------------------


@pytest.mark.parametrize("access_status,with_profile", [
    ("pending", False), ("allowed", False), ("allowed", True), ("pending", True),
])
async def test_admin_converts_an_agentless_pi(
    db_session, orcid_records, persona_dir, access_status, with_profile,
):
    tree_before = _tree(persona_dir)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status=access_status)
    if with_profile:
        await factories.make_profile(db_session, user=pi)

    shown, (user, converted) = await _confirm(db_session, actor=admin, orcid=pi.orcid,
                                              role=USER_ROLE_MANAGER)
    assert shown.outcome == "convert" and shown.user_id == pi.id
    assert shown.access_status == access_status and shown.has_profile is with_profile
    assert converted is True and user.id == pi.id
    row = await _stored(db_session, pi.id, User.user_role, User.access_status,
                        User.session_epoch, User.name)
    assert tuple(row) == (USER_ROLE_MANAGER, "allowed", 1, pi.name)
    if with_profile:  # the profile is kept
        assert await db_session.scalar(
            select(func.count()).select_from(ResearcherProfile)
            .where(ResearcherProfile.user_id == pi.id)
        ) == 1
    (event,) = await _audits(db_session, AUDIT_CONVERT)
    assert event.payload == {"user_id": str(pi.id), "orcid": pi.orcid, "name": pi.name,
                             "role": USER_ROLE_MANAGER}
    assert orcid_records["_calls"] == []  # a convert never fetches ORCID
    assert _tree(persona_dir) == tree_before  # no persona file


async def test_manager_converts_a_pending_pi_with_no_profile(db_session, orcid_records):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    _shown, (_user, converted) = await _confirm(db_session, actor=mgr, orcid=pi.orcid,
                                                role=USER_ROLE_REVIEWER)
    assert converted is True
    row = await _stored(db_session, pi.id, User.user_role, User.access_status, User.session_epoch)
    assert tuple(row) == (USER_ROLE_REVIEWER, "allowed", 1)
    (event,) = await _audits(db_session, AUDIT_CONVERT)
    assert event.actor_user_id == mgr.id


async def _delegate_of_another_lab(db, user):
    owner = await factories.make_user(db)
    agent = await factories.make_agent(db, user=owner)
    db.add(AgentDelegate(agent_registry_id=agent.id, user_id=user.id))
    await db.flush()


@pytest.mark.parametrize("setup", [
    "allowed", "profile", "agent-active", "agent-pending", "agent-inactive", "agent-suspended",
    "delegate",
])
async def test_manager_is_told_to_ask_an_admin(db_session, orcid_records, setup):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(
        db_session, orcid=_new_oid(),
        access_status="allowed" if setup == "allowed" else "pending",
    )
    if setup == "profile":
        await factories.make_profile(db_session, user=pi)
    elif setup.startswith("agent-"):
        await factories.make_agent(db_session, user=pi, status=setup.removeprefix("agent-"))
    elif setup == "delegate":
        await _delegate_of_another_lab(db_session, pi)

    exc = await _refused(staff_accounts.preview(
        db_session, actor=mgr, orcid=pi.orcid, role=USER_ROLE_REVIEWER))
    assert exc.code == "ask_admin" and exc.user_id is None
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=mgr, orcid=pi.orcid, role=USER_ROLE_REVIEWER, expectation="x"))
    assert exc.code == "ask_admin"
    assert (await _stored(db_session, pi.id, User.user_role)).user_role == USER_ROLE_PI


@pytest.mark.parametrize("setup", [
    "agent-active", "agent-pending", "agent-inactive", "agent-suspended", "delegate",
])
async def test_admin_is_sent_to_the_account_page_for_an_agent_or_delegate(
    db_session, orcid_records, setup,
):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid())
    if setup == "delegate":
        await _delegate_of_another_lab(db_session, pi)
    else:
        await factories.make_agent(db_session, user=pi, status=setup.removeprefix("agent-"))

    exc = await _refused(staff_accounts.preview(
        db_session, actor=admin, orcid=pi.orcid, role=USER_ROLE_MANAGER))
    assert exc.code == "use_account_page" and exc.user_id == pi.id
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=admin, orcid=pi.orcid, role=USER_ROLE_MANAGER, expectation="x"))
    assert exc.code == "use_account_page" and exc.user_id == pi.id
    assert await _audits(db_session) == []


@pytest.mark.parametrize("job_type", PER_USER_JOB_TYPES)
@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_running_per_user_work_refuses_a_convert(db_session, orcid_records, job_type, status):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    db_session.add(Job(type=job_type, status=status, user_id=pi.id, payload={}))
    await db_session.flush()
    exc = await _refused(staff_accounts.preview(
        db_session, actor=admin, orcid=pi.orcid, role=USER_ROLE_REVIEWER))
    assert exc.code == "jobs_running"


async def test_finished_work_does_not_refuse_a_convert(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    db_session.add(Job(type="generate_profile", status="completed", user_id=pi.id, payload={}))
    await db_session.flush()
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=pi.orcid,
                                         role=USER_ROLE_REVIEWER)
    assert shown.outcome == "convert"


@pytest.mark.parametrize("role,access_status", [
    (USER_ROLE_PI, "denied"),
    (USER_ROLE_MANAGER, "allowed"),
    (USER_ROLE_REVIEWER, "allowed"),
    (USER_ROLE_ADMIN, "allowed"),
])
@pytest.mark.parametrize("actor_role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER])
async def test_a_denied_or_staff_holder_is_refused_as_exists(
    db_session, orcid_records, role, access_status, actor_role,
):
    actor = await factories.make_user(db_session, user_role=actor_role)
    holder = await factories.make_user(db_session, orcid=_new_oid(), user_role=role,
                                       access_status=access_status)
    exc = await _refused(staff_accounts.preview(
        db_session, actor=actor, orcid=holder.orcid, role=USER_ROLE_REVIEWER))
    assert exc.code == "exists"


async def test_a_pasted_orcid_url_finds_the_holder(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    shown = await staff_accounts.preview(
        db_session, actor=admin, orcid=f"https://orcid.org/{pi.orcid}", role=USER_ROLE_REVIEWER)
    assert shown.outcome == "convert" and shown.orcid == pi.orcid


@pytest.mark.parametrize("stored_name", ["", "ORCID"])
async def test_an_unusable_stored_name_requires_a_typed_one(
    db_session, orcid_records, stored_name,
):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    pi = await factories.make_user(db_session, orcid=oid, access_status="pending",
                                   name=oid if stored_name == "ORCID" else stored_name)

    exc = await _refused(staff_accounts.preview(
        db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER))
    assert exc.code == "name_required"
    exc = await _refused(staff_accounts.preview(
        db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER, name="Ada <b>"))
    assert exc.code == "invalid_name"

    shown, _ = await _confirm(db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER,
                              name="Ada Lovelace")
    assert shown.name == "Ada Lovelace"
    assert (await _stored(db_session, pi.id, User.name)).name == "Ada Lovelace"


async def test_a_usable_stored_name_ignores_a_typed_one(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending",
                                   name="Grace Hopper")
    await _confirm(db_session, actor=admin, orcid=pi.orcid, role=USER_ROLE_REVIEWER,
                   name="Someone Else")
    assert (await _stored(db_session, pi.id, User.name)).name == "Grace Hopper"


# ---- expectation: changed --------------------------------------------------------------


async def _assert_changed(db_session, *, actor, orcid, role, expectation, name=None):
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=actor, orcid=orcid, role=role, name=name, expectation=expectation))
    assert exc.code == "changed" and isinstance(exc.preview, StaffPreview)
    assert exc.preview.expectation() != expectation
    assert await _audits(db_session) == []
    return exc.preview


async def test_changed_when_the_role_differs_from_the_preview(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=oid,
                                         role=USER_ROLE_REVIEWER)
    fresh = await _assert_changed(db_session, actor=admin, orcid=oid, role=USER_ROLE_MANAGER,
                                  expectation=shown.expectation())
    assert fresh.role == USER_ROLE_MANAGER
    assert await _users_with_orcid(db_session, oid) == 0


async def test_changed_when_the_access_status_changed_elsewhere(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=pi.orcid,
                                         role=USER_ROLE_REVIEWER)
    await _write_elsewhere(db_session, pi.id, access_status="allowed")
    fresh = await _assert_changed(db_session, actor=admin, orcid=pi.orcid,
                                  role=USER_ROLE_REVIEWER, expectation=shown.expectation())
    assert fresh.access_status == "allowed"
    assert (await _stored(db_session, pi.id, User.user_role)).user_role == USER_ROLE_PI


async def test_changed_when_a_profile_appeared(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=pi.orcid,
                                         role=USER_ROLE_REVIEWER)
    await factories.make_profile(db_session, user=pi)
    fresh = await _assert_changed(db_session, actor=admin, orcid=pi.orcid,
                                  role=USER_ROLE_REVIEWER, expectation=shown.expectation())
    assert fresh.has_profile is True


async def test_changed_when_the_orcid_name_changed(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=oid,
                                         role=USER_ROLE_REVIEWER)
    orcid_records[oid] = _record(oid, name="Janet Doe")
    fresh = await _assert_changed(db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER,
                                  expectation=shown.expectation())
    assert fresh.name == "Janet Doe"
    assert await _users_with_orcid(db_session, oid) == 0


async def test_changed_when_the_orcid_employments_changed(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=oid,
                                         role=USER_ROLE_REVIEWER)
    orcid_records[oid] = _record(oid, employments=[
        {"organization": "Stanford University", "start_year": 2021, "current": True},
    ])
    fresh = await _assert_changed(db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER,
                                  expectation=shown.expectation())
    assert fresh.employments == (
        {"organization": "Stanford University", "start_year": 2021, "current": True},
    )
    assert fresh.name == shown.name and fresh.institution == shown.institution
    assert await _users_with_orcid(db_session, oid) == 0


async def test_changed_when_the_stored_name_changed_elsewhere(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=pi.orcid,
                                         role=USER_ROLE_REVIEWER)
    await _write_elsewhere(db_session, pi.id, name="Renamed Person")
    fresh = await _assert_changed(db_session, actor=admin, orcid=pi.orcid,
                                  role=USER_ROLE_REVIEWER, expectation=shown.expectation())
    assert fresh.name == "Renamed Person"


async def test_changed_when_the_outcome_changed(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    oid = _new_oid()
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=oid,
                                         role=USER_ROLE_REVIEWER)
    assert shown.outcome == "create"
    pi = await factories.make_user(db_session, orcid=oid, access_status="pending")
    fresh = await _assert_changed(db_session, actor=admin, orcid=oid, role=USER_ROLE_REVIEWER,
                                  expectation=shown.expectation())
    assert fresh.outcome == "convert" and fresh.user_id == pi.id
    assert (await _stored(db_session, pi.id, User.user_role)).user_role == USER_ROLE_PI


# ---- stale identity map -----------------------------------------------------------------


async def test_convert_reads_the_locked_row_not_the_session_copy(db_session, orcid_records):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=pi.orcid,
                                         role=USER_ROLE_REVIEWER)
    await _write_elsewhere(db_session, pi.id, user_role=USER_ROLE_MANAGER)
    assert pi.user_role == USER_ROLE_PI  # the session's copy is stale
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=admin, orcid=pi.orcid, role=USER_ROLE_REVIEWER,
        expectation=shown.expectation()))
    assert exc.code == "exists"
    assert (await _stored(db_session, pi.id, User.user_role)).user_role == USER_ROLE_MANAGER


async def test_convert_lock_reloads_a_row_changed_after_the_unlocked_read(
    db_session, orcid_records, monkeypatch,
):
    """The change lands between the holder select and ``_lock_user``, so only the locked
    load's ``populate_existing`` can see it."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, orcid=_new_oid(), access_status="pending")
    shown = await staff_accounts.preview(db_session, actor=admin, orcid=pi.orcid,
                                         role=USER_ROLE_REVIEWER)
    real_lock = staff_accounts._lock_user
    locked: list[uuid.UUID] = []

    async def write_then_lock(db, user_id):
        await _write_elsewhere(db_session, user_id, user_role=USER_ROLE_MANAGER)
        assert pi.user_role == USER_ROLE_PI  # the session's copy is stale
        locked.append(user_id)
        return await real_lock(db, user_id)

    monkeypatch.setattr(staff_accounts, "_lock_user", write_then_lock)
    exc = await _refused(staff_accounts.create_or_convert(
        db_session, actor=admin, orcid=pi.orcid, role=USER_ROLE_REVIEWER,
        expectation=shown.expectation()))
    assert locked == [pi.id]
    assert exc.code == "exists"
    assert (await _stored(db_session, pi.id, User.user_role)).user_role == USER_ROLE_MANAGER
    assert await _audits(db_session) == []


async def test_revoke_reads_the_locked_row_not_the_session_copy(db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                    last_login_at=datetime.now(UTC))
    await _write_elsewhere(db_session, rev.id, access_status="denied")
    exc = await _refused(staff_accounts.revoke_reviewer(db_session, actor=mgr, user_id=rev.id))
    assert exc.code == "not_allowed"

    rev2 = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                     last_login_at=datetime.now(UTC))
    await _write_elsewhere(db_session, rev2.id, user_role=USER_ROLE_PI)
    exc = await _refused(staff_accounts.revoke_reviewer(db_session, actor=mgr, user_id=rev2.id))
    assert exc.code == "not_reviewer"
    assert await _audits(db_session) == []


async def test_delete_reads_the_locked_row_not_the_session_copy(db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await _write_elsewhere(db_session, rev.id, user_role=USER_ROLE_MANAGER)
    exc = await _refused(staff_accounts.delete_reviewer(db_session, actor=mgr, user_id=rev.id))
    assert exc.code == "not_reviewer"
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == rev.id)) == 1
    assert await _audits(db_session) == []


# ---- revoke / restore / delete ----------------------------------------------------------


@pytest.mark.parametrize("action", ["revoke", "restore", "delete"])
@pytest.mark.parametrize("target_role", [USER_ROLE_PI, USER_ROLE_MANAGER, USER_ROLE_ADMIN])
@pytest.mark.parametrize("actor_role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER])
async def test_reviewer_actions_refuse_non_reviewers(db_session, action, target_role, actor_role):
    actor = await factories.make_user(db_session, user_role=actor_role)
    status = "denied" if action == "restore" else "allowed"
    target = await factories.make_user(db_session, user_role=target_role, access_status=status)
    call = getattr(staff_accounts, f"{action}_reviewer")
    exc = await _refused(call(db_session, actor=actor, user_id=target.id))
    assert exc.code == "not_reviewer"
    assert await _audits(db_session) == []


@pytest.mark.parametrize("action", ["revoke", "restore", "delete"])
async def test_reviewer_actions_refuse_a_missing_account_and_self(db_session, action):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    call = getattr(staff_accounts, f"{action}_reviewer")
    exc = await _refused(call(db_session, actor=mgr, user_id=uuid.uuid4()))
    assert exc.code == "not_found"

    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                    access_status="denied" if action == "restore" else "allowed")
    exc = await _refused(call(db_session, actor=rev, user_id=rev.id))
    assert exc.code == "self"

    # Under impersonation the real admin's own account is "self" too.
    real = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                     access_status=rev.access_status)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    worn._real_admin = real  # type: ignore[attr-defined]
    exc = await _refused(call(db_session, actor=worn, user_id=real.id))
    assert exc.code == "self"
    assert await _audits(db_session) == []


async def test_revoke_denies_and_bumps_the_epoch(db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, session_epoch=4,
                                    last_login_at=datetime.now(UTC))
    returned = await staff_accounts.revoke_reviewer(db_session, actor=mgr, user_id=rev.id)
    assert returned.id == rev.id
    row = await _stored(db_session, rev.id, User.access_status, User.session_epoch)
    assert tuple(row) == ("denied", 5)
    (event,) = await _audits(db_session, AUDIT_REVOKE)
    assert event.actor_user_id == mgr.id
    assert event.payload == {"user_id": str(rev.id), "orcid": rev.orcid, "name": rev.name,
                             "role": USER_ROLE_REVIEWER}


@pytest.mark.parametrize("status", ["denied", "pending"])
async def test_revoke_needs_an_allowed_reviewer(db_session, status):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, access_status=status,
                                    last_login_at=datetime.now(UTC))
    exc = await _refused(staff_accounts.revoke_reviewer(db_session, actor=mgr, user_id=rev.id))
    assert exc.code == "not_allowed"


@pytest.mark.parametrize("status", ["allowed", "denied", "pending"])
async def test_manager_may_not_revoke_a_reviewer_who_never_signed_in(db_session, status):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, access_status=status,
                                    session_epoch=3, last_login_at=None)
    exc = await _refused(staff_accounts.revoke_reviewer(db_session, actor=mgr, user_id=rev.id))
    assert exc.code == "never_signed_in"
    row = await _stored(db_session, rev.id, User.access_status, User.session_epoch)
    assert tuple(row) == (status, 3)
    assert await _audits(db_session) == []


async def test_admin_revokes_a_reviewer_who_never_signed_in(db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, session_epoch=1,
                                    last_login_at=None)
    await staff_accounts.revoke_reviewer(db_session, actor=admin, user_id=rev.id)
    row = await _stored(db_session, rev.id, User.access_status, User.session_epoch)
    assert tuple(row) == ("denied", 2)
    (event,) = await _audits(db_session, AUDIT_REVOKE)
    assert event.actor_user_id == admin.id


async def test_restore_allows_and_queues_nothing(db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                    access_status="denied", session_epoch=2)
    await staff_accounts.restore_reviewer(db_session, actor=mgr, user_id=rev.id)
    row = await _stored(db_session, rev.id, User.access_status, User.session_epoch)
    assert tuple(row) == ("allowed", 2)
    assert await db_session.scalar(
        select(func.count()).select_from(Job).where(Job.user_id == rev.id)) == 0
    (event,) = await _audits(db_session, AUDIT_RESTORE)
    assert event.actor_user_id == mgr.id


@pytest.mark.parametrize("status", ["allowed", "pending"])
async def test_restore_needs_a_denied_reviewer(db_session, status):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, access_status=status)
    exc = await _refused(staff_accounts.restore_reviewer(db_session, actor=mgr, user_id=rev.id))
    assert exc.code == "not_denied"


async def test_delete_keeps_reviews_drops_assignments_and_chat_and_tears_down_an_agent(
    db_session, monkeypatch, tmp_path,
):
    """The actor is an admin: the reviewer owns an agent, which Q5 refuses to a manager."""
    from src.services import user_deletion
    from src.services.slack_tokens import is_valid_token

    public_dir = tmp_path / "public"
    public_dir.mkdir()
    monkeypatch.setattr(user_deletion, "_PUBLIC_DIR", public_dir)
    monkeypatch.setattr(user_deletion, "_COMPANIES_DIR", tmp_path / "companies")
    monkeypatch.setattr(user_deletion, "_MEMORY_DIR", tmp_path / "mem")
    revoked: list[str] = []

    async def fake_revoke(token):
        revoked.append(token)
        return True

    monkeypatch.setattr(user_deletion, "revoke_token_async", fake_revoke)
    token = "xoxb-staff-delete-teardown"
    assert is_valid_token(token)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Rae Viewer")
    agent = await factories.make_agent(db_session, user=rev, status="suspended",
                                       slack_bot_token=token)
    persona = public_dir / f"{agent.agent_id}.md"
    persona.write_text("persona")
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird",
                                       channel_name="staff-delete")
    db_session.add(assessment)
    db_session.add(AccessAllowlist(orcid=rev.orcid))
    await db_session.flush()
    review = AssessmentReview(assessment_id=assessment.id, reviewer_user_id=rev.id,
                              reviewer_name=rev.name, score=3, feedback_mode="log_only")
    db_session.add(review)
    db_session.add(AssessmentReviewAssignment(
        assessment_id=assessment.id, assignee_user_id=rev.id, assignee_name=rev.name,
        assigned_by_name=mgr.name))
    db_session.add(AssessmentChatTurn(
        id=uuid.uuid4(), assessment_id=assessment.id, user_id=rev.id, context_tier="reviewer",
        question="q", status="complete", model="claude-opus-5-5",
        record_sha256_12="0" * 12, prompt_sha256_12="1" * 12, created_at=datetime.now(UTC)))
    await db_session.flush()
    rev_id, rev_orcid, review_id, agent_pk = rev.id, rev.orcid, review.id, agent.id

    report = await staff_accounts.delete_reviewer(db_session, actor=mgr, user_id=rev_id)

    assert report.agent_id == agent.agent_id and report.allowlist_removed is True
    assert not persona.exists() and str(persona) in report.files_deleted
    assert revoked == [token] and report.slack_token_revoked is True
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == rev_id)) == 0
    kept = (await db_session.execute(
        select(AssessmentReview.reviewer_user_id, AssessmentReview.reviewer_name)
        .where(AssessmentReview.id == review_id))).one()
    assert tuple(kept) == (None, "Rae Viewer")
    assert await db_session.scalar(
        select(func.count()).select_from(AssessmentReviewAssignment)
        .where(AssessmentReviewAssignment.assessment_id == assessment.id)) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AssessmentChatTurn)
        .where(AssessmentChatTurn.assessment_id == assessment.id)) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AccessAllowlist)
        .where(AccessAllowlist.orcid == rev_orcid)) == 0
    agent_row = (await db_session.execute(
        select(AgentRegistry.user_id, AgentRegistry.status, AgentRegistry.slack_bot_token)
        .where(AgentRegistry.id == agent_pk))).one()
    assert tuple(agent_row) == (None, "suspended", None)
    (event,) = await _audits(db_session, AUDIT_DELETE)
    assert event.actor_user_id == mgr.id
    assert event.payload == {"user_id": str(rev_id), "orcid": rev_orcid, "name": "Rae Viewer",
                             "role": USER_ROLE_REVIEWER}


# ---- lists ------------------------------------------------------------------------------


async def test_list_staff_accounts_rows_and_flags(db_session):
    viewer = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mm Viewer")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Aa Admin")
    allowed = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Bb Rev",
                                        last_login_at=datetime.now(UTC))
    denied = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                       access_status="denied", name="Cc Rev")
    pending = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                        access_status="pending", name="Dd Rev")
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Ab Pi")
    await factories.make_agent(db_session, user=denied, status="suspended")
    mine = {viewer.id, admin.id, allowed.id, denied.id, pending.id}

    rows = [r for r in await staff_accounts.list_staff_accounts(db_session, viewer=viewer)
            if r["user"].id in mine | {pi.id}]
    assert [r["user"].id for r in rows] == [admin.id, allowed.id, denied.id, pending.id,
                                            viewer.id]
    by_id = {r["user"].id: r for r in rows}
    flags = ("is_self", "has_agent", "can_revoke", "can_restore", "can_delete")
    assert tuple(by_id[admin.id][f] for f in flags) == (False, False, False, False, False)
    assert tuple(by_id[allowed.id][f] for f in flags) == (False, False, True, False, True)
    assert tuple(by_id[denied.id][f] for f in flags) == (False, True, False, True, False)
    assert by_id[denied.id]["delete_needs_admin"] is True
    assert tuple(by_id[pending.id][f] for f in flags) == (False, False, False, False, True)
    assert tuple(by_id[viewer.id][f] for f in flags) == (True, False, False, False, False)
    assert by_id[allowed.id]["user"].access_status == "allowed"

    # A reviewer who never signed in: only an admin viewer may revoke them.
    unseen = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Ee Rev",
                                       last_login_at=None)
    for who, can_revoke in ((viewer, False), (admin, True)):
        (row,) = [r for r in await staff_accounts.list_staff_accounts(db_session, viewer=who)
                  if r["user"].id == unseen.id]
        assert tuple(row[f] for f in flags) == (False, False, can_revoke, False, True)
    (row,) = [r for r in await staff_accounts.list_staff_accounts(db_session, viewer=admin)
              if r["user"].id == allowed.id]
    assert row["can_revoke"] is True
    (row,) = [r for r in await staff_accounts.list_staff_accounts(db_session, viewer=admin)
              if r["user"].id == denied.id]
    assert row["can_delete"] is True and row["delete_needs_admin"] is False

    # A non-staff viewer gets no actions.
    rows = await staff_accounts.list_staff_accounts(db_session, viewer=allowed)
    assert not any(r["can_revoke"] or r["can_restore"] or r["can_delete"] for r in rows)


async def test_recent_staff_changes_newest_first_with_actor_names(db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mona Manager")
    now = datetime.now(UTC)
    payload = {"user_id": str(uuid.uuid4()), "orcid": "0000-0003-0000-8888",
               "name": "Rae Viewer", "role": USER_ROLE_REVIEWER}
    db_session.add_all([
        AdminAuditEvent(action=AUDIT_CREATE, actor_user_id=mgr.id, payload=payload,
                        created_at=now - timedelta(minutes=2)),
        AdminAuditEvent(action=AUDIT_REVOKE, actor_user_id=None, payload=payload,
                        created_at=now - timedelta(minutes=1)),
        AdminAuditEvent(action="simulation_start", actor_user_id=mgr.id, payload={},
                        created_at=now),
    ])
    await db_session.flush()
    changes = await staff_accounts.recent_staff_changes(db_session)
    assert [c["action"] for c in changes][:2] == [AUDIT_REVOKE, AUDIT_CREATE]
    assert all(c["action"] != "simulation_start" for c in changes)
    assert changes[0] == {"action": AUDIT_REVOKE, "actor_name": None, "target_name": "Rae Viewer",
                          "target_orcid": "0000-0003-0000-8888", "role": USER_ROLE_REVIEWER,
                          "created_at": changes[0]["created_at"]}
    assert changes[1]["actor_name"] == "Mona Manager"
    assert len(await staff_accounts.recent_staff_changes(db_session, limit=1)) == 1


# ---- research-data guard (Q5, R1.7) -----------------------------------------------------


async def _research_reviewer(db, kind: str) -> User:
    rev = await factories.make_user(db, user_role=USER_ROLE_REVIEWER)
    if kind == "profile":
        await factories.make_profile(db, user=rev)
    elif kind == "publication":
        db.add(Publication(user_id=rev.id, pmid="31000001", title="A stored paper"))
        await db.flush()
    else:
        await factories.make_agent(db, user=rev, status="suspended")
    return rev


async def test_research_data_for_reports_profile_publications_and_agent(db_session):
    a = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await factories.make_profile(db_session, user=a)
    db_session.add_all([Publication(user_id=a.id, pmid="31000001", title="One"),
                        Publication(user_id=a.id, pmid="31000002", title="Two")])
    b = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    agent = await factories.make_agent(db_session, user=b, status="suspended")
    c = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await db_session.flush()
    got = await research_data_for(db_session, [a.id, b.id, c.id])
    assert got == {
        a.id: ResearchData(True, 2, None),
        b.id: ResearchData(False, 0, agent.agent_id),
        c.id: ResearchData(False, 0, None),
    }
    assert [got[u.id].has_any for u in (a, b, c)] == [True, True, False]


@pytest.mark.parametrize("kind", ["profile", "publication", "agent"])
async def test_a_manager_may_not_delete_a_reviewer_with_research_data(db_session, kind):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await _research_reviewer(db_session, kind)
    exc = await _refused(staff_accounts.delete_reviewer(db_session, actor=mgr, user_id=rev.id))
    assert exc.code == "has_research_data"
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == rev.id)) == 1
    assert await _audits(db_session, AUDIT_DELETE) == []


async def test_an_admin_deletes_a_reviewer_with_research_data(db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await factories.make_profile(db_session, user=rev)
    db_session.add_all([Publication(user_id=rev.id, pmid="31000001", title="One"),
                        Publication(user_id=rev.id, pmid="31000002", title="Two")])
    await db_session.flush()
    rev_id = rev.id
    await staff_accounts.delete_reviewer(db_session, actor=admin, user_id=rev_id)
    for model, column in ((User, User.id), (ResearcherProfile, ResearcherProfile.user_id),
                          (Publication, Publication.user_id)):
        assert await db_session.scalar(
            select(func.count()).select_from(model).where(column == rev_id)) == 0
    (event,) = await _audits(db_session, AUDIT_DELETE)
    assert event.actor_user_id == admin.id


async def test_a_manager_deletes_a_reviewer_without_research_data(db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    rev_id = rev.id
    await staff_accounts.delete_reviewer(db_session, actor=mgr, user_id=rev_id)
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == rev_id)) == 0
