"""The landscaping pipeline controls (spec §13.4; L28, L43, L55, D68, D70).

Mounted at ``/landscape``; POST only, and the set is pinned by
``tests/integration/landscaping/test_pipeline_controls.py``. The router gate is
``get_staff_user``; the admin-only controls declare ``get_admin_user`` as well. Every control
refuses an impersonated session, and the Origin guard covers the router as it does every other.
Each success returns to the pipeline page with a flash; a refused request is an HTTP error, as on
``/reviews``.

The controls write ``app_settings`` (``landscape_paused``, ``landscape_phase``,
``landscape_positive_controls``, ``landscape_experts``) or call the pipeline's own entry points;
the landscaping worker reads them on its next pass.
"""

import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_admin_user, get_staff_user, refuse_impersonation
from src.landscaping import epochs, rescue
from src.landscaping.review_batches import EXPERTS_KEY
from src.models import AppSetting, User
from src.models.landscape import LandscapeReport
from src.services.identifiers import normalize_doi, normalize_pmid
from src.web.flash import flash

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_staff_user)])

_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)
_ADMIN = Depends(get_admin_user)

#: Where every control returns (the staff pipeline page).
_PIPELINE_PAGE = "/workspace/landscape/pipeline"

_PAUSED_KEY = "landscape_paused"
_PHASE_KEY = "landscape_phase"
_CONTROLS_KEY = "landscape_positive_controls"
#: ``landscape_phase`` when unset (spec §12.3).
_DEFAULT_PHASE = "pilot"

_REFUSAL = "Pipeline controls are disabled while impersonating."


def _done(request: Request, text: str) -> RedirectResponse:
    flash(request, text, "success")
    return RedirectResponse(_PIPELINE_PAGE, status_code=303)


async def _read_setting(db: AsyncSession, key: str) -> str | None:
    """The setting's value with whitespace and one pair of surrounding quotes stripped, as the
    worker reads it; None when unset."""
    value = await db.scalar(select(AppSetting.value).where(AppSetting.key == key))
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return value


async def _write_setting(db: AsyncSession, key: str, value: str) -> None:
    """Upsert one ``app_settings`` row, as the worker's ``_set_app_setting`` does."""
    await db.execute(
        pg_insert(AppSetting)
        .values(key=key, value=value)
        .on_conflict_do_update(
            index_elements=[AppSetting.key], set_={"value": value, "updated_at": func.now()}
        )
    )


def _required_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required")
    return reason


@router.post("/pause")
async def pause(request: Request, db: AsyncSession = _DB, current_user: User = _STAFF):
    refuse_impersonation(current_user, _REFUSAL)
    await _write_setting(db, _PAUSED_KEY, "true")
    await db.commit()
    logger.info("Landscaping paused by %s (%s)", current_user.name, current_user.id)
    return _done(request, "Landscaping paused.")


@router.post("/resume")
async def resume(request: Request, db: AsyncSession = _DB, current_user: User = _STAFF):
    refuse_impersonation(current_user, _REFUSAL)
    await _write_setting(db, _PAUSED_KEY, "false")
    await db.commit()
    logger.info("Landscaping resumed by %s (%s)", current_user.name, current_user.id)
    return _done(request, "Landscaping resumed.")


@router.post("/backfill/start")
async def start_backfill(request: Request, db: AsyncSession = _DB, current_user: User = _ADMIN):
    """Move from ``pilot`` to ``backfill``: refused (409) in any other phase, or before a
    ``pilot`` report is stored (spec §13.4)."""
    refuse_impersonation(current_user, _REFUSAL)
    phase = (await _read_setting(db, _PHASE_KEY) or _DEFAULT_PHASE).lower()
    if phase != "pilot":
        raise HTTPException(
            status_code=409, detail=f"Backfill starts only from pilot (now {phase})"
        )
    has_pilot_report = await db.scalar(
        select(exists().where(LandscapeReport.kind == "pilot"))
    )
    if not has_pilot_report:
        raise HTTPException(status_code=409, detail="Backfill needs a stored pilot report")
    await _write_setting(db, _PHASE_KEY, "backfill")
    await db.commit()
    logger.info("Landscaping backfill started by %s (%s)", current_user.name, current_user.id)
    return _done(request, "Backfill started.")


@router.post("/bakeoff/run")
async def run_bakeoff(request: Request, db: AsyncSession = _DB, current_user: User = _ADMIN):
    """Record a ``bakeoff_request`` report for the worker to pick up. While an earlier request is
    still unhandled, nothing new is recorded, so a second press does not buy a second run."""
    refuse_impersonation(current_user, _REFUSAL)
    pending = await db.scalar(
        select(
            exists().where(
                LandscapeReport.kind == "bakeoff_request", LandscapeReport.handled_at.is_(None)
            )
        )
    )
    if pending:
        flash(request, "A bake-off is already requested.", "info")
        return RedirectResponse(_PIPELINE_PAGE, status_code=303)
    db.add(LandscapeReport(
        kind="bakeoff_request",
        payload={"requested_by": str(current_user.id), "requested_by_name": current_user.name},
        # Distinct per report of a kind (uq_landscape_reports_kind_created), even when two
        # requests share a transaction start.
        created_at=func.clock_timestamp(),
    ))
    await db.commit()
    logger.info("Landscaping bake-off requested by %s (%s)", current_user.name, current_user.id)
    return _done(request, "Bake-off requested.")


@router.post("/epochs/{epoch_id}/override")
async def override_epoch(
    epoch_id: uuid.UUID,
    request: Request,
    reason: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Override a held (or not yet regressed) epoch, with a required ``reason`` (L28)."""
    refuse_impersonation(current_user, _REFUSAL)
    reason = _required_reason(reason)
    try:
        await epochs.override(db, epoch_id, actor_id=current_user.id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Landscaping epoch %s overridden by %s (%s)", epoch_id, current_user.name, current_user.id
    )
    return _done(request, "Epoch overridden.")


@router.post("/rescue")
async def rescue_item(
    request: Request,
    item_kind: str = Form(...),
    item_id: str = Form(...),
    stage: str = Form(...),
    expected_status: str = Form(...),
    reason: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Rescue one excluded item (L43, L44). ``item_kind`` and ``stage`` pair as
    ``rescue.rescue`` requires; ``expected_status`` is the exclusion the page showed, so a row
    that changed since is a 409, not a double rescue; ``reason`` is required."""
    refuse_impersonation(current_user, _REFUSAL)
    reason = _required_reason(reason)
    try:
        parsed_id = uuid.UUID(item_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed item id") from exc
    try:
        await rescue.rescue(
            db, item_kind=item_kind, item_id=parsed_id, stage=stage,
            expected_status=expected_status, actor=current_user, reason=reason,
        )
    except rescue.RescueConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Landscaping %s %s rescued at %s by %s (%s)",
        item_kind, parsed_id, stage, current_user.name, current_user.id,
    )
    return _done(request, "Item rescued.")


def _control_identifiers(text: str) -> list[str]:
    """Each DOI or PMID in ``text`` (separated by whitespace, commas or semicolons), normalized
    as ``intake._parse_controls`` reads them and deduplicated; 400 naming the first token that is
    neither."""
    out: list[str] = []
    for token in (t for t in re.split(r"[\s,;]+", text) if t):
        if doi := normalize_doi(token):
            out.append(doi.lower())
        elif pmid := normalize_pmid(token):
            out.append(pmid)
        else:
            raise HTTPException(status_code=400, detail=f"Neither a DOI nor a PMID: {token[:80]}")
    return list(dict.fromkeys(out))


@router.post("/positive-controls")
async def set_positive_controls(
    request: Request,
    identifiers: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Replace the positive-control list with the DOIs and PMIDs in ``identifiers``; blank
    clears it. Stored in the database, never in git (spec §13.4)."""
    refuse_impersonation(current_user, _REFUSAL)
    values = _control_identifiers(identifiers)
    await _write_setting(db, _CONTROLS_KEY, json.dumps(values))
    await db.commit()
    logger.info(
        "Landscaping positive controls set to %d identifiers by %s (%s)",
        len(values), current_user.name, current_user.id,
    )
    return _done(request, f"{len(values)} positive controls saved.")


async def _expert_ids_for_update(db: AsyncSession) -> list[str]:
    """The stored expert ids, with the row locked so two concurrent marks do not lose one. An
    unreadable value is logged and treated as empty."""
    raw = await db.scalar(
        select(AppSetting.value).where(AppSetting.key == EXPERTS_KEY).with_for_update()
    )
    try:
        values = json.loads(raw) if raw else []
    except ValueError:
        values = None
    if not isinstance(values, list):
        logger.warning("%s is not a JSON list; starting from no experts", EXPERTS_KEY)
        return []
    return [str(v) for v in values]


@router.post("/experts")
async def set_expert(
    request: Request,
    user_id: str = Form(...),
    action: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """``action`` ``mark`` or ``unmark`` the account ``user_id`` as an expert (L55). Only a
    reviewer or staff account with allowed access can be marked; unmarking takes any stored id."""
    refuse_impersonation(current_user, _REFUSAL)
    if action not in ("mark", "unmark"):
        raise HTTPException(status_code=400, detail="action must be 'mark' or 'unmark'")
    try:
        target_id = uuid.UUID(user_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed user id") from exc
    ids = await _expert_ids_for_update(db)
    if action == "mark":
        target = await db.get(User, target_id)
        if target is None or not (
            (target.is_staff or target.is_reviewer) and target.access_status == "allowed"
        ):
            raise HTTPException(
                status_code=400,
                detail="an expert must be a staff member or reviewer with allowed access",
            )
        if str(target_id) not in ids:
            ids.append(str(target_id))
    else:
        ids = [i for i in ids if i != str(target_id)]
    await _write_setting(db, EXPERTS_KEY, json.dumps(ids))
    await db.commit()
    logger.info(
        "Landscaping expert %s %sed by %s (%s)",
        target_id, action, current_user.name, current_user.id,
    )
    return _done(request, "Expert marked." if action == "mark" else "Expert unmarked.")
