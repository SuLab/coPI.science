"""``scripts/scripps_admin.py``: accounts, the backfill start, epoch override, source disable
and the batch probe (spec 2026-11-20 §6.6, §7, C7, C9). Names and iDs are invented; the
script's commits are savepoint releases under ``db_session``."""
import contextlib
import uuid

import anthropic
import httpx
import pytest
from sqlalchemy import select

from scripts import scripps_admin
from src.models import AppSetting, User
from src.models.landscape import LandscapeEpoch, LandscapeReport, LandscapeSource
from src.routers.landscape import BackfillRefused

pytestmark = pytest.mark.integration

OWNER_ORCID = "0000-0002-1825-0097"
#: Second and third checksum-valid iDs (ISO 7064 11,2).
OTHER_ORCIDS = ("0000-0001-5109-3700", "0000-0003-1415-9269")


async def _users(db, orcid: str) -> list[User]:
    return list((await db.execute(
        select(User).where(User.orcid == orcid).execution_options(populate_existing=True)
    )).scalars())


async def _phase(db) -> str | None:
    return await db.scalar(select(AppSetting.value).where(AppSetting.key == "landscape_phase"))


async def test_grant_creates_an_allowed_admin(db_session):
    await scripps_admin.grant(db_session, orcid=OWNER_ORCID, role="admin", name="Invented Owner")
    [user] = await _users(db_session, OWNER_ORCID)
    assert user.user_role == "admin"
    assert user.access_status == "allowed"
    assert user.onboarding_complete is True


async def test_grant_creates_a_manager_or_reviewer_without_onboarding(db_session):
    for orcid, role in zip(OTHER_ORCIDS, ("manager", "reviewer"), strict=True):
        await scripps_admin.grant(db_session, orcid=orcid, role=role, name="Invented Colleague")
        [user] = await _users(db_session, orcid)
        assert user.user_role == role
        assert user.access_status == "allowed"
        assert user.onboarding_complete is False


async def test_grant_updates_an_existing_account(db_session):
    db_session.add(User(
        orcid=OWNER_ORCID, name="Invented Pending", user_role="pi",
        access_status="pending", onboarding_complete=False, session_epoch=None,
    ))
    await db_session.flush()
    await scripps_admin.grant(db_session, orcid=OWNER_ORCID, role="admin", name="Invented Owner")
    [user] = await _users(db_session, OWNER_ORCID)
    assert user.user_role == "admin"
    assert user.access_status == "allowed"
    assert user.onboarding_complete is True
    assert user.session_epoch == 1


async def test_grant_refuses_a_bad_orcid_or_role(db_session):
    with pytest.raises(ValueError):
        await scripps_admin.grant(
            db_session, orcid="0000-0002-1825-0098", role="admin", name="Invented Owner"
        )
    with pytest.raises(ValueError):
        await scripps_admin.grant(db_session, orcid=OWNER_ORCID, role="pi", name="Invented Owner")
    with pytest.raises(ValueError):
        await scripps_admin.grant(db_session, orcid=OWNER_ORCID, role="admin", name="  ")
    assert await _users(db_session, OWNER_ORCID) == []


async def test_start_backfill_refused_without_pilot_report(db_session):
    with pytest.raises(BackfillRefused, match="Backfill needs a stored pilot report"):
        await scripps_admin.start_backfill(db_session)
    assert await _phase(db_session) is None


async def test_start_backfill_refused_outside_pilot(db_session):
    db_session.add(LandscapeReport(kind="pilot", payload={}))
    db_session.add(AppSetting(key="landscape_phase", value="continuous"))
    await db_session.flush()
    with pytest.raises(
        BackfillRefused, match=r"Backfill starts only from pilot \(now continuous\)"
    ):
        await scripps_admin.start_backfill(db_session)
    assert await _phase(db_session) == "continuous"


async def test_start_backfill_moves_pilot_to_backfill(db_session):
    db_session.add(LandscapeReport(kind="pilot", payload={}))
    await db_session.flush()
    await scripps_admin.start_backfill(db_session)
    assert await _phase(db_session) == "backfill"


async def test_override_epoch(db_session):
    admin = await scripps_admin.grant(
        db_session, orcid=OWNER_ORCID, role="admin", name="Invented Owner"
    )
    epoch = LandscapeEpoch(status="held")
    db_session.add(epoch)
    await db_session.flush()

    with pytest.raises(ValueError):
        await scripps_admin.override_epoch(
            db_session, epoch_id=epoch.id, actor_orcid=OTHER_ORCIDS[0], reason="invented reason"
        )
    with pytest.raises(ValueError):
        await scripps_admin.override_epoch(
            db_session, epoch_id=epoch.id, actor_orcid=OWNER_ORCID, reason="   "
        )
    await scripps_admin.grant(
        db_session, orcid=OTHER_ORCIDS[1], role="manager", name="Invented Colleague"
    )
    with pytest.raises(ValueError, match="only an admin"):
        await scripps_admin.override_epoch(
            db_session, epoch_id=epoch.id, actor_orcid=OTHER_ORCIDS[1], reason="invented reason"
        )
    await db_session.refresh(epoch)
    assert epoch.status == "held"

    await scripps_admin.override_epoch(
        db_session, epoch_id=epoch.id, actor_orcid=OWNER_ORCID, reason="invented reason"
    )
    await db_session.refresh(epoch)
    assert epoch.status == "candidate"
    assert epoch.override_actor_id == admin.id
    assert epoch.override_reason == "invented reason"


async def test_disable_source(db_session):
    await scripps_admin.disable_source(db_session, source="geo", reason="invented")
    row = await db_session.scalar(
        select(LandscapeSource).where(LandscapeSource.source == "geo")
        .execution_options(populate_existing=True)
    )
    assert row.enabled is False
    with pytest.raises(ValueError):
        await scripps_admin.disable_source(db_session, source="nope", reason="invented")


class _Batches:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.cancelled: list[str] = []
        self.created: list[dict] = []

    async def create(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.created.append(kwargs)
        return type("Batch", (), {"id": "b1"})()

    async def cancel(self, batch_id):
        self.cancelled.append(batch_id)


class _Client:
    def __init__(self, batches: _Batches):
        self.messages = type("Messages", (), {"batches": batches})()


async def test_batch_probe():
    batches = _Batches()
    assert await scripps_admin.batch_probe(_Client(batches), "invented-model") == ("api", "b1")
    assert batches.cancelled == ["b1"]
    [request] = batches.created[0]["requests"]
    assert request["custom_id"] == "scripps-batch-probe"
    assert request["params"]["max_tokens"] == 1

    denied = anthropic.PermissionDeniedError(
        "no batches",
        response=httpx.Response(
            403, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
        ),
        body=None,
    )
    mode, detail = await scripps_admin.batch_probe(_Client(_Batches(denied)), "invented-model")
    assert mode == "sync" and detail


def test_main_exits_2_on_a_refusal(monkeypatch, capsys):
    @contextlib.asynccontextmanager
    async def no_session():
        yield None

    # The reason is refused before the session is used, so no database is reached.
    monkeypatch.setattr(scripps_admin, "_session_factory", lambda: no_session)
    assert scripps_admin.main(
        ["override-epoch", "--epoch", str(uuid.uuid4()), "--actor-orcid", OWNER_ORCID,
         "--reason", "  "]
    ) == 2
    assert "reason" in capsys.readouterr().err
