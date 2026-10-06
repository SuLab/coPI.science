"""0060: pi_grant_identity, pi_orcid_fundings, pi_grants.vetoed_by_user_id and the jobs
rerun columns; their FK rules (CASCADE on user_id, SET NULL on actors) under a raw
DELETE FROM users; the downgrade drops them. The database is at head (tests/conftest.py)."""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.enrichment import GRANT_IDENTITY_STATUSES
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _m0060():
    spec = importlib.util.spec_from_file_location(
        "_m0060", _VERSIONS / "0060_grant_identity_orcid_fundings_job_reruns.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    """Run 0060's upgrade or downgrade on the session's own connection, so the test's
    outer transaction rolls every statement back."""
    mod = _m0060()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session, table: str) -> set[str]:
    return set((await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
    ), {"t": table})).scalars())


async def _scalar(db_session, sql: str, params: dict):
    return (await db_session.execute(text(sql), params)).scalar_one()


def test_the_status_check_lists_the_model_statuses():
    assert tuple(_m0060().STATUSES) == GRANT_IDENTITY_STATUSES


async def test_raw_user_delete_cascades_owned_rows_and_nulls_actor_columns(db_session):
    pi = await factories.make_user(db_session)
    staff = await factories.make_user(db_session, user_role="manager")
    await db_session.execute(text(
        "INSERT INTO pi_grant_identity (user_id, status, pinned_profile_ids, pinned_by_user_id) "
        "VALUES (:u, 'pinned', ARRAY[1,2], :s)"
    ), {"u": pi.id, "s": staff.id})
    await db_session.execute(text(
        "INSERT INTO pi_orcid_fundings "
        "(id, user_id, group_key, title, vetoed_at, vetoed_by_user_id) "
        "VALUES (:i, :u, 'hash:x', 'T', now(), :s)"
    ), {"i": uuid.uuid4(), "u": pi.id, "s": staff.id})
    await db_session.execute(text(
        "INSERT INTO pi_grants (id, user_id, core_project_num, title, org_name, "
        "tenure_filter_mode, is_subproject, vetoed_at, vetoed_by_user_id) "
        "VALUES (:i, :u, 'R01X', 'T', 'JHU', 'org_only', false, now(), :s)"
    ), {"i": uuid.uuid4(), "u": pi.id, "s": staff.id})

    await db_session.execute(text("DELETE FROM users WHERE id = :s"), {"s": staff.id})
    owner = {"u": pi.id}
    assert await _scalar(
        db_session, "SELECT pinned_by_user_id FROM pi_grant_identity WHERE user_id = :u", owner
    ) is None
    assert await _scalar(
        db_session, "SELECT vetoed_by_user_id FROM pi_orcid_fundings WHERE user_id = :u", owner
    ) is None
    assert await _scalar(
        db_session, "SELECT vetoed_by_user_id FROM pi_grants WHERE user_id = :u", owner
    ) is None

    await db_session.execute(text("DELETE FROM users WHERE id = :u"), owner)
    for table in ("pi_grant_identity", "pi_orcid_fundings", "pi_grants"):
        assert await _scalar(
            db_session, f"SELECT count(*) FROM {table} WHERE user_id = :u", owner
        ) == 0


async def test_one_identity_row_per_pi_and_unique_group_keys(db_session):
    pi = await factories.make_user(db_session)
    insert_identity = text("INSERT INTO pi_grant_identity (user_id) VALUES (:u)")
    await db_session.execute(insert_identity, {"u": pi.id})
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(insert_identity, {"u": pi.id})

    insert_funding = text(
        "INSERT INTO pi_orcid_fundings (id, user_id, group_key, title) "
        "VALUES (:i, :u, 'k', :t)"
    )
    await db_session.execute(insert_funding, {"i": uuid.uuid4(), "u": pi.id, "t": "T"})
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert_funding, {"i": uuid.uuid4(), "u": pi.id, "t": "T2"}
            )


async def test_status_check_refuses_an_unknown_status_and_none_confirmed_defaults_false(
    db_session,
):
    pi = await factories.make_user(db_session)
    await db_session.execute(
        text("INSERT INTO pi_grant_identity (user_id) VALUES (:u)"), {"u": pi.id}
    )
    assert await _scalar(
        db_session, "SELECT none_confirmed FROM pi_grant_identity WHERE user_id = :u",
        {"u": pi.id},
    ) is False
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text(
                "UPDATE pi_grant_identity SET status = 'maybe' WHERE user_id = :u"
            ), {"u": pi.id})


async def test_downgrade_drops_everything_and_upgrade_restores_it(db_session):
    await _run(db_session, "downgrade")
    assert "name_sanitized_at" not in await _columns(db_session, "users")
    assert "rerun_requested_at" not in await _columns(db_session, "jobs")
    assert "rerun_not_before" not in await _columns(db_session, "jobs")
    assert "rerun_priority" not in await _columns(db_session, "jobs")
    assert "vetoed_by_user_id" not in await _columns(db_session, "pi_grants")
    assert await _columns(db_session, "pi_grant_identity") == set()
    assert await _columns(db_session, "pi_orcid_fundings") == set()
    await _run(db_session, "upgrade")
    assert {"rerun_requested_at", "rerun_not_before", "rerun_priority"} <= await _columns(
        db_session, "jobs"
    )
    assert "none_confirmed" in await _columns(db_session, "pi_grant_identity")
    assert "group_key" in await _columns(db_session, "pi_orcid_fundings")
    assert "vetoed_by_user_id" in await _columns(db_session, "pi_grants")
    assert "name_sanitized_at" in await _columns(db_session, "users")
