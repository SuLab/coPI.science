import pytest

from src.services import directory
from tests.unit._frozen_list_assessments import list_assessments as frozen_list
from tests.unit._seeded_assessments import seeded_assessments  # noqa: F401

pytestmark = pytest.mark.integration

_ARGS = [
    dict(run_id=None, sort=None, review=None, lab=None),
    dict(run_id="all", sort="score", review="reviewed", lab=None),
    dict(run_id="all", sort="newest", review="unreviewed", lab="lab1"),
    dict(run_id="not-a-uuid", sort="bogus", review="bogus", lab="nobody"),
]


@pytest.mark.parametrize("kw", _ARGS)
async def test_equal(db_session, seeded_assessments, kw):  # noqa: F811
    run_id = kw.pop("run_id")
    new = await directory.list_assessments(db_session, run_id, **kw)
    old = await frozen_list(db_session, run_id, **kw)
    assert new == old
