"""Every key the Projects pages' read models return (cards, tiles, timeline, evidence,
assignments and the rest) survives the page projection: for a reviewer all but
``ADMIN_KEYS``, for an admin all of them."""

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
                "rubric_version": None,
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
                "anchor": "p-1",
                "lab": "lab1",
                "title": "A title",
                "outcome_kind": "not_finished",
                "outcome_label": "Not finished",
                "reason_label": "Research failed",
                "is_finished": True,
                "requested_at": NOW,
                "posted_at": NOW,
                "finished_at": NOW,
                "summary": "A summary",
                "key_points": [{"label": "Indication", "value": "fibrosis"}],
                "hub_position": "Agreed",
                "lab_position": "No position stated",
                "gap_count": 1,
                "disagreement_count": 0,
                "reviewers": [{"name": "Reviewer", "rating": 4, "fund": True}],
                "review_count": 2,
                "mean_rating": 3.5,
                "fund_count": 1,
                "reviewed_by_me": True,
                "my_review": {"rating": 4, "fund": True},
                "stage": None,
                "stage_label": None,
                "stage_paused": False,
                "stage_time": None,
                "stage_time_label": None,
                "next_id": "",
                "assigned_names": ["Reviewer"],
            }
        ],
        "project_total": 1,
        "lab_options": ["lab1"],
        "lab_filter": "lab1",
        "outcome_filter": "not_finished",
        "review_filter": "all",
        "sort": "lab",
        "mine": False,
        "assignment_filter": "mine",
        "list_state": {
            "run_id": str(run_id),
            "lab": "lab1",
            "outcome": "not_finished",
            "review": "all",
            "sort": "lab",
            "mine": "1",
            "assignment": "mine",
        },
        "review_counts": {"unreviewed": 0, "reviewed": 1, "all": 1},
        "tiles": [{"kind": "total", "label": "Total", "count": 1}],
        "not_finished_reasons": [{"label": "Research failed", "count": 1}],
        "summary": {
            "counts": {"ready": 1, "not_finished": 1},
            "review_count": 2,
            "mean_rating": 3.5,
            "fund_share": 0.5,
        },
        "pi_user_ids": {"lab1": str(uuid.uuid4())},
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
        "edited": False,
        "can_edit": True,
    }
    return {
        "list_state": {"lab": "lab1", "review": "all"},
        "project": {
            "id": uuid.uuid4(),
            "lab": "lab1",
            "pi_user_id": str(uuid.uuid4()),
            "title": "A title",
            "summary": "A summary",
            "outcome_kind": "ready",
            "outcome_label": "Ready",
            "reason_label": None,
            "is_finished": True,
            "run_id": uuid.uuid4(),
            "run_started_at": NOW,
            "requested_at": NOW,
            "posted_at": NOW,
            "finished_at": NOW,
            "has_close": True,
            "why_chosen": "Because",
            "others": [{"use": "Another use", "note": "Weaker"}],
            "anchors": [{"section": "Model Systems", "item": "zebrafish"}],
            "plan_title": "Plan",
            "plan_rows": [
                {
                    "label": "Indication",
                    "value": "fibrosis",
                    "changed": True,
                    "opening_value": "scarring",
                }
            ],
            "tool_rows": [{"item": "zebrafish models", "in_profile": True, "matched": "zebrafish"}],
            "hub_position": "Agreed",
            "lab_position": "Agreed",
            "gaps": ["A gap"],
            "disagreements": ["A disagreement"],
            "sources_by_kind": [
                {
                    "kind": "papers",
                    "label": "Papers",
                    "items": [
                        {
                            "ident": "PMID 31500698",
                            "title": "A paper",
                            "url": "https://pubmed.ncbi.nlm.nih.gov/31500698/",
                            "specialist": "scientific",
                        }
                    ],
                }
            ],
            "evidence": [
                {
                    "domain": "scientific",
                    "anchor": "consult-1",
                    "established": ["E"],
                    "concerns": ["C"],
                    "questions_to_ask": ["A?"],
                    "truncated": False,
                    "earlier_count": 0,
                }
            ],
            "timeline": [
                {
                    "kind": "posting",
                    "anchor": "m-1",
                    "speaker": "BlackbirdBot",
                    "at": NOW,
                    "text": "Posting",
                    "partial": False,
                },
                {
                    "kind": "post",
                    "anchor": "m-2",
                    "speaker": "Lab1Bot",
                    "at": NOW,
                    "text": "Reply",
                    "closed_thread": False,
                },
                {
                    "kind": "consult",
                    "anchor": "consult-1",
                    "domain": "scientific",
                    "question": "Q?",
                    "established": ["E"],
                    "concerns": ["C"],
                    "questions_to_ask": ["A?"],
                    "concern_count": 1,
                    "truncated": True,
                    "at": NOW,
                },
            ],
            "thread_unavailable": False,
            "counts": {"sources": 1, "consults": 1, "posts": 2, "others": 1},
            "reviews": [review],
            "my_review": dict(review),
            "can_review_projects": True,
            "assignments": [{"assignee_user_id": uuid.uuid4(), "assignee_name": "Reviewer"}],
            "review_capable_users": [{"id": uuid.uuid4(), "name": "Reviewer"}],
            "research_cost_usd": Decimal("1.25"),
            "refinement_cost_usd": Decimal("0.50"),
            "calls_url": "/admin/activity/x/llm-calls?thread=1.000100",
            "raw_choice": {"title": "A title", "plan": {"indication": "scarring"}, "others": []},
            "raw_final": {"plan": {"indication": "fibrosis"}, "hub_position": "agree"},
        },
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


def test_reviewer_projection_drops_raw_json_costs_and_calls_url():
    """A reviewer's detail projection has no raw choice, raw close, costs or calls link."""
    projected = _project(_detail_view(), "reviewer")["project"]
    for key in ("raw_choice", "raw_final", "research_cost_usd", "refinement_cost_usd",
                "calls_url"):
        assert key not in projected, key


def test_admin_projection_keeps_raw_json_whole():
    """An admin's projection keeps every stored key of the raw choice and close."""
    view = _detail_view()
    projected = _project(view, "admin")["project"]
    assert dict(projected["raw_choice"]) == view["project"]["raw_choice"]
    assert dict(projected["raw_final"]) == view["project"]["raw_final"]
