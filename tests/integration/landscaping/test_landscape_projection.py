"""Every key the Landscape templates read survives the page projection
(``src/web/presentation.py``), on the real read models of the rendered-page gate's seed.

A key missing from the projection's allowlists is dropped silently and renders blank
(presentation.py's module docstring), so each page's context is built as its route builds it,
projected for the viewer, and compared key by key with the unprojected context. A dropped key
fails the test when the Landscape templates name it (any identifier inside a Jinja ``{{ }}`` or
``{% %}`` block of ``templates/workspace/landscape*.html`` and ``templates/landscape/*.html``),
unless the projection drops it on purpose for this viewer: ``SECRET_KEYS`` always,
``ADMIN_KEYS`` for a non-admin, ``STAFF_KEYS`` for a reviewer. Mirrors
``tests/unit/test_projects_projection.py``, on database-built views rather than literals.
"""
import re
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from src.models.landscape import LandscapeDecision
from src.routers.workspace.landscape import _form_choices, _reason_tags
from src.services import landscape_view
from src.services.blackbird_rubric import BANDING
from src.services.web_permissions import capabilities_for
from src.web.presentation import ADMIN_KEYS, SECRET_KEYS, STAFF_KEYS, project_workspace
from tests import factories
from tests.integration.landscaping.test_landscape_pages import (
    seed_cut,
    seed_held_then_overridden,
    seed_reject_audit,
    seed_source,
)
from tests.integration.test_rendered_page_gate import seed_landscape_opportunity

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATES = (
    sorted((_ROOT / "templates" / "workspace").glob("landscape*.html"))
    + sorted((_ROOT / "templates" / "landscape").glob("*.html"))
)
_JINJA_BLOCK = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)


def _template_names() -> set[str]:
    """Every identifier, attribute name and quoted word inside the Landscape templates' Jinja
    blocks: a superset of the keys they read (``card.cost_line``, ``vs.get('thresholds')``,
    ``expert['items']`` and top-level names alike). Comments (``{# #}``) are not blocks."""
    names: set[str] = set()
    for path in _TEMPLATES:
        for block in _JINJA_BLOCK.finditer(path.read_text()):
            names.update(re.findall(r"[A-Za-z_]\w*", block.group(1) or block.group(2)))
    return names


def _dropped(view, projected, path: str = "") -> Iterator[tuple[str, str]]:
    """``(path, key)`` of every key `view` holds at any depth that `projected` lacks there."""
    if isinstance(view, Mapping):
        if not isinstance(projected, Mapping):
            yield path, "<not a mapping>"
            return
        for key, value in view.items():
            where = f"{path}.{key}"
            if key in projected:
                yield from _dropped(value, projected[key], where)
            else:
                yield where, str(key)
    elif isinstance(view, (list, tuple)):
        for n, (value, out) in enumerate(zip(view, projected, strict=True)):
            yield from _dropped(value, out, f"{path}[{n}]")


def _keys(view) -> set[str]:
    """Every mapping key in `view`, at any depth."""
    if isinstance(view, Mapping):
        return {str(k) for k in view} | {k for v in view.values() for k in _keys(v)}
    if isinstance(view, (list, tuple)):
        return {k for v in view for k in _keys(v)}
    return set()


async def _world(db):
    """The gate seed plus what the pipeline page and the lists' cut sentence show, and the
    manager's quick decision (so ``decisions``, ``my_decision`` and ``decided_by`` carry
    values)."""
    opportunity_id = await seed_landscape_opportunity(db)
    await seed_cut(db, opportunity_id)
    await seed_source(db)
    await seed_reject_audit(db)
    await seed_held_then_overridden(db)
    users = {
        role: await factories.make_user(db, user_role=role)
        for role in (USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN)
    }
    manager = users[USER_ROLE_MANAGER]
    db.add(LandscapeDecision(
        id=uuid.uuid4(), opportunity_id=opportunity_id, reviewer_id=manager.id,
        reviewer_name=manager.name, decision="pursue", reason_tags=["Team & executability"],
        reason="Gate reason.",
    ))
    await db.flush()
    return opportunity_id, users


async def _contexts(db, user, opportunity_id) -> dict[str, dict]:
    """Each Landscape page's content for `user`, as its route passes it to
    ``workspace_context`` (src/routers/workspace/landscape.py)."""
    common = {"reason_tag_choices": _reason_tags(), "banding": BANDING}
    state = {"view": "current", "tab": "unreviewed"}
    pages = {
        "batch": {
            **common, "selected_view": "batch", "lists": None, "list_state": {},
            "batch": await landscape_view.batch_view(db, user),
            "expert": await landscape_view.expert_view(db, user),
        },
        "detail": {
            **common, "chat_enabled": False, "chat_available": False, "list_state": state,
            **_form_choices(),
            **await landscape_view.detail_view(db, user, opportunity_id),
        },
    }
    if user.user_role != USER_ROLE_REVIEWER:
        pages["current"] = {
            **common, "selected_view": "current", "batch": None, "list_state": state,
            "lists": await landscape_view.lists_view(
                db, user, view="current", tab="all", assignment="all",
            ),
            "expert": await landscape_view.expert_view(db, user),
        }
        pages["pipeline"] = await landscape_view.pipeline_view(db, user)
    return pages


async def test_every_template_key_registered(db_session):
    opportunity_id, users = await _world(db_session)
    names = _template_names()
    # Teeth: the scan found the templates and reads their new keys.
    assert len(_TEMPLATES) >= 9
    assert {"cost_line", "elevator_pitch", "flags", "not_raised", "quotes", "kind_label",
            "idea_n", "was_held", "latest_cut_opportunities", "tiles", "lab_options"} <= names

    problems: list[str] = []
    seen: set[str] = set()
    for role, user in users.items():
        permissions = capabilities_for(user)
        expected = set(SECRET_KEYS)
        if not permissions.admin:
            expected |= ADMIN_KEYS
        if not permissions.staff:
            expected |= STAFF_KEYS
        for page, content in (await _contexts(db_session, user, opportunity_id)).items():
            seen |= _keys(content)
            projected = project_workspace("landscape", content, permissions)
            problems += [
                f"{role} {page}: {where}"
                for where, key in _dropped(content, projected)
                if key in names and key not in expected
            ]
    # Teeth: the views carried the keys the remediation added, so the comparison saw them.
    assert {"cost_line", "elevator_pitch", "decided_by", "flags", "not_raised", "quotes",
            "kind_label", "idea_n", "was_held", "latest_cut_opportunities", "cut", "tiles",
            "lab_options", "confirmed_not_red_flagged", "verdict_revision", "review_rubric",
            "flag_evidence_raw", "database_evidence_raw", "label"} <= seen
    assert not problems, "keys the Landscape templates read are dropped by the projection:\n" + (
        "\n".join(problems)
    )
