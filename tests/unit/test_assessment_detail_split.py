import itertools
from types import SimpleNamespace

import pytest

from src.services import assessment_detail as ad
from src.services.rubric_revisions import PROVENANCE_LIVE, PROVENANCE_UNKNOWN
from tests.unit import _frozen_assessment_detail as frozen
from tests.unit._seeded_assessments import seeded_assessments  # noqa: F401

_REV = SimpleNamespace(scale_max=5, scale_min=1)
_A = [
    SimpleNamespace(gating={"ip_clear": "met", "x": "not_met", "y": "unconfirmed", "z": True}, red_flags=["f"],
                    dimension_rationales={"team": "why"}, scores={"team": 5}),
    SimpleNamespace(gating=None, red_flags="nope", dimension_rationales=None, scores=None),
    SimpleNamespace(gating={}, red_flags=[], dimension_rationales={}, scores={}),
]
_DIMS = [None, [], [{"key": "team", "title": "Team", "score": 4.8}, {"key": "b", "score": 1.0},
                    {"key": "c", "score": None}, {"key": "d", "score": 3}, "junk"]]
_CONSULTS = [
    None, [],
    [{"domain": "legal", "verdict_signal": "blocking", "concerns": ["a", "b"], "created_at": 2},
     {"domain": "legal", "verdict_signal": "adequate", "established": ["c"], "created_at": 1},
     {"domain": "clinical", "verdict_signal": "high", "created_at": 3},
     {"domain": "ip", "verdict_signal": "gap", "reply_truncated": True},
     {"domain": "reg", "verdict_signal": "gap", "read_state": "defaulted"}],
]


@pytest.mark.parametrize("a,dims,consults,rev,prov", list(itertools.product(
    _A, _DIMS, _CONSULTS, [_REV, None], [PROVENANCE_LIVE, PROVENANCE_UNKNOWN, None])))
def test_strengths_and_risks_equal(a, dims, consults, rev, prov):
    kw = dict(dimensions=dims, consults=consults, revision=rev, revision_provenance=prov)
    assert ad.derive_strengths_and_risks(a, **kw) == frozen.derive_strengths_and_risks(a, **kw)


@pytest.mark.integration
@pytest.mark.parametrize("admin_view,viewer_is_staff", [(True, True), (False, False), (False, True)])
async def test_build_assessment_detail_equal(db_session, seeded_assessments, admin_view, viewer_is_staff):  # noqa: F811
    for assessment_id in seeded_assessments:
        new = await ad.build_assessment_detail(db_session, assessment_id, admin_view=admin_view,
                                               viewer_is_staff=viewer_is_staff)
        old = await frozen.build_assessment_detail(db_session, assessment_id, admin_view=admin_view,
                                                   viewer_is_staff=viewer_is_staff)
        assert new == old
