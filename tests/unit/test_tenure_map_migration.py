"""One-time migration of the curated agent_id-keyed tenure map to per-user rows.

The 2026-08-13 map (`app_settings` key ``jhu_tenure_start``) is keyed by
agent_id, which agentless pipeline runs don't have (audit M1). The migration
rewrites it into ``jhu_tenure_start:{user_id}`` rows with
``source="curated-2026-08-13"``, reporting agent_ids it cannot resolve, and
never clobbers an existing per-user entry (a manual correction outranks the
curated import).
"""

import json

import pytest

from scripts.migrate_tenure_map import migrate_tenure_map
from src.models import AppSetting
from src.services.jhu_rules import TENURE_KEY_PREFIX, get_tenure_start
from tests import factories

pytestmark = pytest.mark.integration


async def test_migrates_resolvable_entries_and_reports_the_rest(db_session):
    user = await factories.make_user(db_session, orcid="0000-0009-3333-4444")
    await factories.make_agent(
        db_session, user=user, agent_id="wu", bot_name="WuBot"
    )
    db_session.add(
        AppSetting(
            key="jhu_tenure_start",
            value=json.dumps({"wu": 2009, "ghost": 2015}),
        )
    )
    await db_session.flush()

    report = await migrate_tenure_map(db_session)

    assert report["migrated"] == 1
    assert report["unresolved"] == ["ghost"]
    assert await get_tenure_start(db_session, user.id) == 2009

    row = await db_session.get(AppSetting, f"{TENURE_KEY_PREFIX}{user.id}")
    assert json.loads(row.value)["source"] == "curated-2026-08-13"


async def test_rerun_is_idempotent_and_never_clobbers_an_existing_entry(db_session):
    user = await factories.make_user(db_session, orcid="0000-0009-5555-6666")
    await factories.make_agent(
        db_session, user=user, agent_id="lee", bot_name="LeeBot"
    )
    db_session.add(
        AppSetting(key="jhu_tenure_start", value=json.dumps({"lee": 2010}))
    )
    # A manual correction already exists for this user.
    db_session.add(
        AppSetting(
            key=f"{TENURE_KEY_PREFIX}{user.id}",
            value=json.dumps(
                {"year": 2013, "source": "manual", "derived_at": "2026-08-24"}
            ),
        )
    )
    await db_session.flush()

    report = await migrate_tenure_map(db_session)

    assert report["migrated"] == 0
    assert report["skipped"] == 1
    assert await get_tenure_start(db_session, user.id) == 2013
