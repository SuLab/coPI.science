"""``src/services/tenure_scope.py`` — the one definition of "inside the JHU
tenure window" (Task 11 of
``docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md``, §3B
D17-D20).

Pure-function tests cover the rule itself: the inclusive boundary, undated
exclusion (counted, never silently dropped, D19), and the ``tenure_start is
None`` full-career pass-through (D20). The DB-backed test covers the one
thing a pure test cannot: that ``tenure_start_map`` honours the legacy
agent-keyed map when a PI has no per-user entry — omitting that fallback
silently unscopes the 62 PIs who predate the per-user key.
"""

import dataclasses
import json

import pytest

from src.models import AppSetting
from src.services.jhu_rules import LEGACY_TENURE_KEY, set_tenure_start
from src.services.tenure_scope import (
    ScopedPublications,
    partition_publications,
    scoped_counts,
    tenure_start_map,
)
from tests import factories

pytestmark = pytest.mark.integration


class _FakePub:
    def __init__(self, year):
        self.year = year


def test_inclusive_boundary_keeps_the_tenure_start_year_itself():
    pubs = [_FakePub(2019), _FakePub(2020), _FakePub(2021)]
    scoped = partition_publications(pubs, tenure_start=2020)
    assert [p.year for p in scoped.publications] == [2020, 2021]
    assert scoped.before_tenure == 1
    assert scoped.undated_excluded == 0
    assert scoped.scoped is True
    assert scoped.count == 2
    assert scoped.excluded == 1


def test_undated_rows_are_excluded_but_counted_not_dropped():
    pubs = [_FakePub(2020), _FakePub(None), _FakePub(None), _FakePub(2015)]
    scoped = partition_publications(pubs, tenure_start=2018)
    assert [p.year for p in scoped.publications] == [2020]
    assert scoped.before_tenure == 1
    assert scoped.undated_excluded == 2
    # Every row is accounted for somewhere: none silently vanished.
    assert scoped.count + scoped.before_tenure + scoped.undated_excluded == len(pubs)


def test_undated_rows_are_identity_when_no_tenure_start_is_set():
    """D20: with no tenure year at all, even an undated row passes through —
    there is no window to exclude it from."""
    pubs = [_FakePub(2020), _FakePub(None)]
    scoped = partition_publications(pubs, tenure_start=None)
    assert scoped.publications == pubs
    assert scoped.before_tenure == 0
    assert scoped.undated_excluded == 0
    assert scoped.scoped is False
    assert scoped.tenure_start is None


def test_partition_preserves_input_order():
    pubs = [_FakePub(2022), _FakePub(2019), _FakePub(2021)]
    scoped = partition_publications(pubs, tenure_start=2020)
    assert [p.year for p in scoped.publications] == [2022, 2021]


def test_scoped_publications_is_a_frozen_dataclass_instance():
    scoped = partition_publications([], tenure_start=2020)
    assert isinstance(scoped, ScopedPublications)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scoped.tenure_start = 1999  # frozen: assignment must fail


async def test_tenure_start_map_uses_the_legacy_agent_map_when_per_user_is_absent(
    db_session,
):
    """The 2026-08-13 curated map is keyed by agent_id, not user_id. A PI
    with only a legacy entry (no ``jhu_tenure_start:{user_id}`` row) must
    still resolve a tenure_start through ``agent_ids`` — omitting that
    fallback is exactly the D17 regression this module exists to prevent."""
    legacy_user = await factories.make_user(db_session, orcid="0000-0001-1111-2222")
    await factories.make_agent(
        db_session, user=legacy_user, agent_id="legacypi", bot_name="LegacyBot"
    )
    per_user_user = await factories.make_user(db_session, orcid="0000-0001-3333-4444")
    await set_tenure_start(per_user_user.id, 2021, "manual", db=db_session)
    db_session.add(
        AppSetting(key=LEGACY_TENURE_KEY, value=json.dumps({"legacypi": 2016}))
    )
    await db_session.flush()

    starts = await tenure_start_map(
        db_session,
        [legacy_user.id, per_user_user.id],
        agent_ids={legacy_user.id: "legacypi", per_user_user.id: None},
    )
    assert starts[legacy_user.id] == 2016
    assert starts[per_user_user.id] == 2021


async def test_tenure_start_map_omitting_agent_ids_drops_the_legacy_fallback(
    db_session,
):
    """The failure mode the fallback exists to prevent, made explicit: with
    no ``agent_ids`` passed at all, a legacy-only PI resolves to None."""
    user = await factories.make_user(db_session, orcid="0000-0001-5555-6666")
    await factories.make_agent(
        db_session, user=user, agent_id="dropped", bot_name="DroppedBot"
    )
    db_session.add(
        AppSetting(key=LEGACY_TENURE_KEY, value=json.dumps({"dropped": 2016}))
    )
    await db_session.flush()

    starts = await tenure_start_map(db_session, [user.id])
    assert starts[user.id] is None


async def test_scoped_counts_splits_in_before_and_undated_per_user(db_session):
    user = await factories.make_user(db_session, orcid="0000-0002-1111-2222")
    await set_tenure_start(user.id, 2020, "manual", db=db_session)
    from src.models import Publication

    db_session.add_all(
        [
            Publication(user_id=user.id, title="New", year=2021),
            Publication(user_id=user.id, title="Old", year=2015),
            Publication(user_id=user.id, title="Undated", year=None),
        ]
    )
    await db_session.flush()

    counts = await scoped_counts(db_session, [user.id])
    result = counts[user.id]
    assert result.tenure_start == 2020
    assert result.in_tenure == 1
    assert result.before_tenure == 1
    assert result.undated_excluded == 1
    assert result.scoped is True
    assert result.total == 3
