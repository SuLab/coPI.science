"""Every key the Projects pages' read models return survives the page projection: for a
reviewer all but ``ADMIN_KEYS``, for an admin all of them."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from src.models import User
from src.services.web_permissions import capabilities_for
from src.web.presentation import ADMIN_KEYS, project_workspace

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _list_view() -> dict:
    run_id = uuid.uuid4()
    return {
        "run_bar": [
            {
                "id": run_id,
                "started_at": NOW,
                "ended_at": None,
                "rubric_version": "3.5.0",
                "count": 2,
                "is_current": True,
                "is_live": False,
                "badge": "Stopped",
            }
        ],
        "selected_run_id": run_id,
        "projects": [
            {
                "id": uuid.uuid4(),
                "lab": "lab1",
                "title": "A title",
                "outcome_kind": "not_finished",
                "outcome_label": "Not finished",
                "reason_label": "Research failed",
                "finished_at": NOW,
                "review_count": 2,
                "mean_rating": 3.5,
                "fund_count": 1,
                "reviewed_by_me": True,
            }
        ],
        "project_total": 1,
        "lab_options": ["lab1"],
        "lab_filter": "lab1",
        "outcome_filter": "not_finished",
        "mine": False,
        "summary": {
            "counts": {"ready": 1, "not_finished": 1},
            "review_count": 2,
            "mean_rating": 3.5,
            "fund_share": 0.5,
        },
        "can_review_projects": True,
    }


def _detail_view() -> dict:
    review = {
        "id": uuid.uuid4(),
        "reviewer_name": "Reviewer",
        "rating": 4,
        "comment": "Good",
        "fund": True,
        "created_at": NOW,
        "can_edit": True,
    }
    return {
        "project": {
            "id": uuid.uuid4(),
            "lab": "lab1",
            "title": "A title",
            "summary": "A summary",
            "outcome_kind": "ready",
            "outcome_label": "Ready",
            "reason_label": None,
            "run_id": uuid.uuid4(),
            "requested_at": NOW,
            "posted_at": NOW,
            "finished_at": NOW,
            "plan_rows": [{"label": "Indication", "value": "fibrosis"}],
            "opening_rows": [{"label": "Indication", "value": "fibrosis"}],
            "tool_rows": [{"item": "Rosetta", "in_profile": True}],
            "hub_position": "Agreed",
            "lab_position": "Agreed",
            "gaps": ["A gap"],
            "disagreements": ["A disagreement"],
            "why_chosen": "Because",
            "others": [{"use": "Another use", "note": "Weaker"}],
            "anchors": [{"section": "Model Systems", "item": "zebrafish"}],
            "sources": [
                {
                    "kind": "pubmed",
                    "ident": "PMID 31500698",
                    "title": "A paper",
                    "url": "https://pubmed.ncbi.nlm.nih.gov/31500698/",
                    "specialist": "scientific",
                }
            ],
            "timeline": [{"speaker": "BlackbirdBot", "kind": "posting", "text": "Posting", "at": NOW}],
            "consults": [
                {
                    "domain": "scientific",
                    "question": "Q?",
                    "established": ["E"],
                    "concerns": ["C"],
                    "questions_to_ask": ["A?"],
                }
            ],
            "reviews": [review],
            "my_review": dict(review),
            "can_review_projects": True,
            "research_cost_usd": Decimal("1.25"),
            "refinement_cost_usd": Decimal("0.50"),
            "calls_url": "/admin/activity/x/llm-calls?thread=1.000100",
        }
    }


def _shape(value, *, drop=frozenset()):
    """The nested key structure of ``value``, with ``drop`` keys removed at every level."""
    if isinstance(value, dict):
        return {k: _shape(v, drop=drop) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [_shape(v, drop=drop) for v in value]
    return None


def _project(view, role):
    permissions = capabilities_for(User(id=uuid.uuid4(), name="V", user_role=role))
    return project_workspace("projects", view, permissions)


def _projected_shape(projected):
    """``_shape`` over a projection, whose records are Mappings rather than dicts."""
    if isinstance(projected, Mapping):
        return {k: _projected_shape(v) for k, v in projected.items()}
    if isinstance(projected, (list, tuple)):
        return [_projected_shape(v) for v in projected]
    return None


def test_list_keys_survive_for_reviewer_and_admin():
    view = _list_view()
    for role in ("reviewer", "manager", "admin"):
        assert _projected_shape(_project(view, role)) == _shape(view), role


def test_detail_keys_survive_for_admin_entirely():
    view = _detail_view()
    assert _projected_shape(_project(view, "admin")) == _shape(view)


def test_detail_keys_survive_for_reviewer_except_admin_keys():
    view = _detail_view()
    for role in ("reviewer", "manager"):
        projected = _project(view, role)
        assert _projected_shape(projected) == _shape(view, drop=ADMIN_KEYS), role
        for key in ("research_cost_usd", "refinement_cost_usd", "calls_url"):
            assert key not in projected["project"]
