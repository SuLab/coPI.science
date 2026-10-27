"""The simulation control panel's writes, /admin/simulation/*: start, a system's wind-down
or stop, Stop everything, Finalize run and the announcement settings. The page itself is
/workspace/simulation (src/routers/workspace/simulation.py); GET /admin/simulation
redirects there."""

import hashlib
import re
import uuid
from datetime import UTC, datetime

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.run_marker import parse_announce_channels, validate_template
from src.agent.sessions import (
    INTERVIEWS,
    PROJECTS,
    RUNNING,
    SYSTEM_LABELS,
    SYSTEMS,
    WINDING_DOWN,
    Limits,
)
from src.dependencies import impersonation_note
from src.models import AppSetting, SimulationCommand, SimulationRun, User
from src.routers.admin._common import _ADMIN, _DB, router
from src.routers.workspace.compatibility import redirect
from src.routers.workspace.simulation import (
    ANNOUNCE_CHANNELS_KEY,
    ANNOUNCE_TEMPLATE_KEY,
    kv_get,
    render_simulation_page,
    simulation_page_context,
)
from src.services.project_research import research_unavailable_reason
from src.services.runs import latest_run_id
from src.services.simulation_control import (
    SYSTEM_STARTING,
    derive_panel_state,
    engine_alive,
    enqueue_command,
    is_finalize_stop,
    read_status,
    record_audit,
    systems_view,
    takes_system_commands,
)
from src.web.flash import flash
from src.web.urls import page_url

# ---------------------------------------------------------------------------
# /admin/simulation/* — the control-plane panel's writes (Task 7 of
# docs/plans/2026-08-30-simulation-control-panel.md). Command/heartbeat/audit primitives live in
# src.services.simulation_control; this module stays thin — shape a form, ask the
# service a question, redirect to the panel (/workspace/simulation). The
# supervisor (src/agent/supervisor.py) and the running engine's own
# `_poll_control_plane` are the only consumers of the rows written here.
# ---------------------------------------------------------------------------

#: Slack's own channel-name shape (lowercase letters, digits, hyphens,
#: underscores, up to 80 chars) — checked per name AFTER
#: parse_announce_channels has already split/deduped/trimmed the raw input,
#: so an admin typo is caught here rather than surfacing later as a silent
#: channel_not_found from Slack at run-start time.
_ANNOUNCE_CHANNEL_RE = re.compile(r"^[a-z0-9_-]{1,80}$")


def _hash12(text: str | None) -> str | None:
    """First 12 hex chars of a sha256 digest, or None for a None input.

    The ONLY form the announce-template audit payload ever records of a
    template body — never the full text (it may echo an unpublished PI
    disclosure's placeholder shape, or simply be long).
    """
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _audit_payload(payload: dict | None, current_user: User) -> dict | None:
    """The audit row's payload plus ``impersonation_note`` when an admin made this
    request while impersonating another admin (A-10). ``actor_user_id`` stays the
    worn account (operator decision 2026-09-10). Never applied to a command
    payload: the supervisor reads those."""
    note = impersonation_note(current_user)
    if note is None:
        return payload
    return {**(payload or {}), "impersonation_note": note}



async def _kv_upsert(db: AsyncSession, key: str, value: str) -> None:
    stmt = pg_insert(AppSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
    await db.execute(stmt)



async def _kv_delete(db: AsyncSession, key: str) -> None:
    await db.execute(sa_delete(AppSetting).where(AppSetting.key == key))



def _refuse_to(request: Request, url: str, message: str) -> RedirectResponse:
    """Flash ``message`` as an error and redirect to ``url`` (A-14: never in the query)."""
    flash(request, message, "error")
    return RedirectResponse(url=url, status_code=302)


def _panel_url(request: Request) -> str:
    return page_url(request, "workspace_simulation")


@router.get("/simulation", response_class=HTMLResponse, name="legacy_admin_simulation")
async def admin_simulation(request: Request, current_user: User = _ADMIN):
    """The panel moved to /workspace/simulation (owner, 2026-10-27); this keeps the admin
    bar's Simulation link and old bookmarks working, ``?run=`` included."""
    return redirect(request, current_user, "simulation", "workspace_simulation")


_RUN_ID_FORM = Form(...)
#: The largest limit a start takes, count or minutes: far above any real run, and it keeps
#: the engine's ``timedelta(minutes=…)`` in range.
_LIMIT_MAX = 1_000_000
#: C-20: a session limit is a count (proposals for interviews, finished projects for
#: projects) or a runtime in minutes; 0 means "no limit". One object per parameter:
#: FastAPI names a form field after the first parameter a shared ``Form()`` is bound to,
#: so a shared one would read max_runtime into every limit.
_MAX_RUNTIME_FORM = Form(0, ge=0, le=_LIMIT_MAX)
_MAX_PROPOSALS_FORM = Form(0, ge=0, le=_LIMIT_MAX)
_MAX_PROJECTS_FORM = Form(0, ge=0, le=_LIMIT_MAX)
_PROJECTS_RUNTIME_FORM = Form(0, ge=0, le=_LIMIT_MAX)
_FRESH_FORM = Form(False)
_START_SYSTEM_FORM = Form(INTERVIEWS)
_SYSTEM_FORM = Form(...)
_ACTION_FORM = Form(...)

#: The start form's ``system`` value that starts both systems in a run that is not live.
_BOTH = "both"
#: ``POST /admin/simulation/system`` actions, as the engine's ``system`` command reads them.
_WIND_DOWN = "wind_down"
_STOP_NOW = "stop_now"
_PENDING_SYSTEM = "Another interviews or projects request is pending; try again in a moment."
_STARTING_OR_LIVE = "A run is already starting or in progress."
_LEGACY_ENGINE = "This engine predates per-system controls; use Stop everything."


async def _pending(db: AsyncSession, command: str) -> SimulationCommand | None:
    """The pending command of kind ``command`` (the 0042 index allows at most one)."""
    return (
        await db.execute(
            select(SimulationCommand).where(
                SimulationCommand.status == "pending", SimulationCommand.command == command,
            )
        )
    ).scalar_one_or_none()


async def _lock_latest_run(db: AsyncSession) -> SimulationRun | None:
    """The latest run row, locked ``FOR NO KEY UPDATE`` (C-24; see ``admin_simulation_start``)."""
    latest_id = await latest_run_id(db)
    if latest_id is None:
        return None
    return (
        await db.execute(
            select(SimulationRun)
            .where(SimulationRun.id == latest_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()


async def _stopping(db: AsyncSession, status_row) -> bool:
    """The live engine is stopping, or a stop is queued for it. A queued Finalize run is not
    one: a live engine fails it and the run goes on."""
    if status_row is not None and status_row.state == "stopping":
        return True
    pending_stop = await _pending(db, "stop")
    return pending_stop is not None and not is_finalize_stop(pending_stop)


async def _idle_start_refusal(
    db: AsyncSession, chosen: tuple[str, ...], status_row,
) -> str | None:
    """Why a start with no engine alive is refused, or None."""
    if await _pending(db, "start") is not None:
        return _STARTING_OR_LIVE
    if derive_panel_state(status_row, datetime.now(UTC), engine_alive=False) == "starting":
        # The supervisor has claimed a start and its engine has not taken the lock yet.
        return _STARTING_OR_LIVE
    pending_stop = await _pending(db, "stop")
    if pending_stop is not None and is_finalize_stop(pending_stop):
        # The finalize would stamp the run a start is about to resume or replace.
        return "A Finalize run is pending; start after it finishes."
    if PROJECTS in chosen:
        return research_unavailable_reason()
    return None


async def _live_start_refusal(db: AsyncSession, status_row, system: str) -> str | None:
    """Why starting ``system`` (one system, never ``both``) in the live run is refused,
    or None."""
    if await _stopping(db, status_row):
        return "The run is stopping."
    if status_row is None or status_row.simulation_run_id is None:
        return _STARTING_OR_LIVE
    if not takes_system_commands(status_row):
        return _LEGACY_ENGINE
    state = systems_view(status_row, engine_is_alive=True)[system]["state"]
    if state == SYSTEM_STARTING:
        return _STARTING_OR_LIVE
    if state in (RUNNING, WINDING_DOWN):
        return f"{SYSTEM_LABELS[system]} are already running."
    if await _pending(db, "system") is not None:
        return _PENDING_SYSTEM
    if system == PROJECTS:
        return research_unavailable_reason()
    return None


async def _enqueue_system_command(
    request: Request, db: AsyncSession, current_user: User, *,
    payload: dict, audit_action: str, message: str,
) -> RedirectResponse:
    """Enqueue a ``system`` command for the live engine, audit it and flash ``message``.
    The 0042 index's one-pending-per-kind ``IntegrityError`` is the race-proof layer
    behind the pending check."""
    try:
        await enqueue_command(
            db, command="system", payload=payload, requested_by_user_id=current_user.id,
        )
    except IntegrityError:
        await db.rollback()
        return _refuse_to(request, _panel_url(request), _PENDING_SYSTEM)
    await record_audit(
        db, action=audit_action, actor_user_id=current_user.id,
        payload=_audit_payload(payload, current_user),
    )
    flash(request, message, "success")
    return RedirectResponse(url=_panel_url(request), status_code=302)


async def _start_in_live_run(
    request: Request, db: AsyncSession, current_user: User, system: str, limits: Limits,
) -> RedirectResponse:
    """Ask the live engine to start ``system`` in its run (spec §4.3): a ``system``
    command ``{"action": "start", "system", "run_id", "limits"}``. The engine claims it
    at its next control poll; the supervisor finishes it stale if the run ended first."""
    status_row = await read_status(db)
    refusal = await _live_start_refusal(db, status_row, system)
    if refusal is not None:
        return _refuse_to(request, _panel_url(request), refusal)
    payload = {
        "action": "start", "system": system,
        "run_id": str(status_row.simulation_run_id), "limits": limits.as_dict(),
    }
    return await _enqueue_system_command(
        request, db, current_user, payload=payload,
        audit_action="simulation_system_start_requested",
        message=f"Start {system} requested.",
    )


@router.post("/simulation/start")
async def admin_simulation_start(
    request: Request,
    system: str = _START_SYSTEM_FORM,
    fresh: bool = _FRESH_FORM,
    max_runtime: int = _MAX_RUNTIME_FORM,
    max_proposals: int = _MAX_PROPOSALS_FORM,
    max_projects: int = _MAX_PROJECTS_FORM,
    projects_runtime: int = _PROJECTS_RUNTIME_FORM,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Start interviews, projects, or both (``system``: ``interviews``, ``projects``
    or ``both``). Interviews take ``max_proposals``/``max_runtime``, projects
    ``max_projects``/``projects_runtime`` (minutes; 0 = no limit).

    With no engine holding the engine lock (`engine_alive`, spec §8.3): a `start`
    command ``{"fresh", "systems": {name: Limits.as_dict()}}`` for the supervisor to
    claim. Refused while a `start` is already pending (the common double-click case,
    caught here before ever reaching the database) or a Finalize run is pending. The
    0042 partial unique index (`uq_simulation_commands_one_pending`) is the second,
    race-proof layer: an `IntegrityError` from `enqueue_command` is caught and
    rendered as a refusal. DECLARED v1 decision: this form plus these refusals ARE
    the confirmation step — no JS confirm dialog — and the sharp CLI-only flags
    (`--all-agents`/`--reset-cursors`) are deliberately not exposed here.

    With an engine alive: one system joins the live run as a `system` command
    (`_start_in_live_run`); ``both`` is refused, as is a system already starting,
    running or winding down, a stopping engine, or a pending `system` command.

    Projects are refused while ``research_unavailable_reason()`` names a reason.

    Takes ``SELECT … FOR NO KEY UPDATE`` on the run row(s) before its checks (C-24):
    it serializes start and finalize against each other without conflicting with
    the FOR KEY SHARE locks the engine's inserts take on the referenced run row
    (plan audit Q2-06).
    """
    if system not in (*SYSTEMS, _BOTH):
        return _refuse_to(request, _panel_url(request), "Unknown system.")
    limits = {
        INTERVIEWS: Limits(count=max_proposals, runtime_minutes=max_runtime),
        PROJECTS: Limits(count=max_projects, runtime_minutes=projects_runtime),
    }
    # C-24: lock the latest run row before any check. Finalize takes the same
    # lock first (admin_simulation_finalize_run), so a start that would resume
    # this run and a finalize of it serialize: the second waits for the first's
    # commit, then sees its pending command and refuses.
    latest = await _lock_latest_run(db)
    if await engine_alive(db):
        if system == _BOTH:
            return _refuse_to(
                request, _panel_url(request), "Start both applies only when no run is live."
            )
        return await _start_in_live_run(request, db, current_user, system, limits[system])
    chosen = SYSTEMS if system == _BOTH else (system,)
    refusal = await _idle_start_refusal(db, chosen, await read_status(db))
    if refusal is not None:
        return _refuse_to(request, _panel_url(request), refusal)
    if latest is not None and latest.finalized_at is not None:
        # A finalized run refuses a resume (RunFinalized); the form forces Fresh
        # and so does the route, so the operator never queues a doomed start.
        fresh = True
    payload = {"fresh": fresh, "systems": {name: limits[name].as_dict() for name in chosen}}
    try:
        await enqueue_command(
            db, command="start", payload=payload, requested_by_user_id=current_user.id
        )
    except IntegrityError:
        await db.rollback()
        return _refuse_to(request, _panel_url(request), "A start is already pending.")
    await record_audit(
        db, action="simulation_start_requested", actor_user_id=current_user.id,
        payload=_audit_payload(payload, current_user),
    )
    flash(request, "Start requested.", "success")
    return RedirectResponse(url=_panel_url(request), status_code=302)


def _system_stop_refusal(status_row, system: str, action: str) -> str | None:
    """Why ``action`` on ``system`` in the live run is refused, before the pending checks."""
    if status_row is None or status_row.simulation_run_id is None:
        return "Nothing is running."
    if not takes_system_commands(status_row):
        return _LEGACY_ENGINE
    state = systems_view(status_row, engine_is_alive=True)[system]["state"]
    if state not in (RUNNING, WINDING_DOWN):
        return f"{SYSTEM_LABELS[system]} are not running."
    if action == _WIND_DOWN and state == WINDING_DOWN:
        return f"{SYSTEM_LABELS[system]} are already winding down."
    return None


@router.post("/simulation/system")
async def admin_simulation_system(
    request: Request,
    system: str = _SYSTEM_FORM,
    action: str = _ACTION_FORM,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Wind down (``wind_down``: nothing new starts, work under way finishes) or stop
    now (``stop_now``: its open work ends at once) one system of the live run (spec
    §4.5–§4.6): a `system` command ``{"action", "system", "run_id"}`` the live engine
    claims at its next control poll. Refused with no engine alive, for a system that
    is not running or winding down, a wind-down of one already winding down, a
    stopping engine, or while another `system` command is pending. Stop everything
    (`admin_simulation_stop`) is a separate command kind, so a pending `system`
    command never blocks it."""
    if action not in (_WIND_DOWN, _STOP_NOW):
        return _refuse_to(request, _panel_url(request), "Unknown action.")
    if system not in SYSTEMS:
        return _refuse_to(request, _panel_url(request), "Unknown system.")
    if not await engine_alive(db):
        return _refuse_to(request, _panel_url(request), "Nothing is running.")
    status_row = await read_status(db)
    refusal = _system_stop_refusal(status_row, system, action)
    if refusal is None and await _stopping(db, status_row):
        refusal = "The run is stopping."
    if refusal is None and await _pending(db, "system") is not None:
        refusal = _PENDING_SYSTEM
    if refusal is not None:
        return _refuse_to(request, _panel_url(request), refusal)
    payload = {"action": action, "system": system, "run_id": str(status_row.simulation_run_id)}
    message = (f"Wind down {system} requested." if action == _WIND_DOWN
               else f"Stop {system} now requested.")
    return await _enqueue_system_command(
        request, db, current_user, payload=payload,
        audit_action="simulation_system_stop_requested", message=message,
    )


@router.post("/simulation/finalize-run")
async def admin_simulation_finalize_run(
    request: Request,
    run_id: uuid.UUID = _RUN_ID_FORM,
    confirm_run: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Enqueue Finalize run for a stopped run (spec §8.2, B19): a `stop`
    command carrying `{"finalize": true, "run_id": ...}`, the existing enum
    value, so no enum migration. Refused while an engine holds the lock or a
    start is pending; a live engine would only fail it. With no engine alive the
    supervisor claims it and runs the finalize routine under the engine lock.

    The form must also carry ``confirm_run`` equal to the run id's
    first 8 characters (case and surrounding spaces ignored).

    Takes ``SELECT … FOR NO KEY UPDATE`` on the run row(s) before its checks (C-24):
    it serializes start and finalize against each other without conflicting with
    the FOR KEY SHARE locks the engine's inserts take on the referenced run row
    (plan audit Q2-06)."""
    def _refuse(message: str):
        return _refuse_to(request, page_url(request, "workspace_activity_detail", run_id=run_id), message)

    short_id = str(run_id)[:8]
    if confirm_run.strip().lower() != short_id:
        # C-10: the run's short id, typed, is the confirmation; checked here, not only
        # in the browser's dialog.
        return _refuse(f"Type the run's short id ({short_id}) to confirm Finalize run.")

    # C-24: the same latest-run lock admin_simulation_start takes, then this run's
    # row. Always in that order, so the two routes cannot deadlock.
    latest_id = await latest_run_id(db)
    if latest_id is not None and latest_id != run_id:
        await db.execute(
            select(SimulationRun.id)
            .where(SimulationRun.id == latest_id)
            .with_for_update(key_share=True)
        )
    run = (
        await db.execute(
            select(SimulationRun)
            .where(SimulationRun.id == run_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if await engine_alive(db):
        return _refuse("An engine is running — Finalize run applies to a stopped run.")
    pending_start = (
        await db.execute(
            select(SimulationCommand).where(
                SimulationCommand.status == "pending", SimulationCommand.command == "start",
            )
        )
    ).scalar_one_or_none()
    if pending_start is not None:
        return _refuse("A start is pending — wait for it before finalizing.")
    if run is None or run.status != "stopped" or run.finalized_at is not None:
        return _refuse("Only a stopped run that is not already finalized can be finalized.")
    payload = {"finalize": True, "run_id": str(run_id)}
    try:
        await enqueue_command(
            db, command="stop", payload=payload, requested_by_user_id=current_user.id,
        )
    except IntegrityError:
        await db.rollback()
        return _refuse("A stop is already pending.")
    await record_audit(
        db, action="simulation_finalize_requested", actor_user_id=current_user.id,
        payload=_audit_payload(payload, current_user),
    )
    flash(request, "Finalize run requested.", "success")
    return RedirectResponse(url=page_url(request, "workspace_activity_detail", run_id=run_id), status_code=302)


@router.post("/simulation/stop")
async def admin_simulation_stop(
    request: Request,
    hold_open: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Enqueue a `stop` command.

    ``hold_open=1`` is "Stop — hold open interviews": the same `stop` command
    with payload ``{"hold_open": true}``, which makes the engine end the run as
    a hold — only interviews that have ended get their headline, the rest wait
    for a resume or a finalize. The default Stop's payload stays None and its
    behaviour is unchanged.

    Allowed whenever an engine holds the lock (`engine_alive`), including a
    `starting` or unresponsive one (the stop applies at that engine's next
    control poll, as today); refused otherwise — the same predicate the
    supervisor uses (src/agent/supervisor.py), which would only finish a stop
    nobody can act on as "nothing running". The IntegrityError catch is the same
    double-click/race guard as the start route.
    """
    if not await engine_alive(db):
        return _refuse_to(request, _panel_url(request), "Nothing is running.")
    payload = {"hold_open": True} if hold_open == "1" else None
    try:
        await enqueue_command(
            db, command="stop", payload=payload, requested_by_user_id=current_user.id,
        )
    except IntegrityError:
        await db.rollback()
        return _refuse_to(request, _panel_url(request), "A stop is already pending.")
    await record_audit(
        db, action="simulation_stop_requested", actor_user_id=current_user.id,
        payload=_audit_payload(payload, current_user),
    )
    message = "Stop (hold open interviews) requested." if payload else "Stop requested."
    flash(request, message, "success")
    return RedirectResponse(url=_panel_url(request), status_code=302)



@router.post("/simulation/announce-settings")
async def admin_simulation_announce_settings(
    request: Request,
    channels: str = Form(""),
    disable: bool = Form(False),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Persist, disable, or clear the DB override for the run-start
    announcement's channel list.

    Three distinct outcomes, matching the three states the page shows
    (`simulation_page_context`'s `channels_kv`): an explicit `disable` writes the
    override as `""` regardless of whatever is in `channels` — the engine
    already treats an empty-string override as "skip the announcement"
    (`parse_announce_channels("") == []`), so this is not new engine
    behavior, just a page control for a state the engine already supported
    but the page previously had no way to request on purpose (it was only
    reachable by accident, and indistinguishable on the page from "no
    override"). Leaving `disable` unchecked with an EMPTY `channels` field
    still CLEARS the override outright (falls back to the `Settings`
    default) — unchanged from before this control existed. A non-empty
    `channels` field (with `disable` unchecked) sets the override to that
    list, each name checked against Slack's own channel-name shape
    (`_ANNOUNCE_CHANNEL_RE`) first. All three write the same audit action;
    the `new` payload value (`""`, `None`, or a channel list) is what
    distinguishes them after the fact.
    """
    old_value = await kv_get(db, ANNOUNCE_CHANNELS_KEY)

    if disable:
        new_value = ""
        await _kv_upsert(db, ANNOUNCE_CHANNELS_KEY, new_value)
        msg = "Announcements disabled."
    else:
        names = parse_announce_channels(channels)
        bad = [n for n in names if not _ANNOUNCE_CHANNEL_RE.match(n)]
        if bad:
            return _refuse_to(
                request, _panel_url(request), "Invalid channel name(s): " + ", ".join(bad)
            )
        new_value = ",".join(names) or None
        if new_value is None:
            await _kv_delete(db, ANNOUNCE_CHANNELS_KEY)
        else:
            await _kv_upsert(db, ANNOUNCE_CHANNELS_KEY, new_value)
        msg = "Announce channels updated."

    await record_audit(
        db,
        action="simulation_announce_channels_updated",
        actor_user_id=current_user.id,
        payload=_audit_payload({"old": old_value, "new": new_value}, current_user),
    )
    flash(request, msg, "success")
    return RedirectResponse(url=_panel_url(request), status_code=302)



@router.post("/simulation/announce-template")
async def admin_simulation_announce_template(
    request: Request,
    body: str = Form(""),
    reset: bool = Form(False),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Save (or reset) the DB override for the run-start announcement body.

    `reset` deletes the KV row outright and is never validated (deleting
    can't introduce a bad template) — it falls back to the template FILE,
    not to `DEFAULT_TEMPLATE` directly (see `run_marker.template_body`).
    Otherwise the body is checked with `run_marker.validate_template` BEFORE
    anything is written: a bad body re-renders this same page with the
    error inline and the just-submitted text still in the textarea, rather
    than redirecting — the one route here that isn't a plain
    POST-then-redirect, because the error needs the submitted body back in
    front of the admin, not a fresh KV read. The audit payload never stores
    the template text itself, only a sha256[:12] fingerprint of the old and
    new bodies.
    """
    old_value = await kv_get(db, ANNOUNCE_TEMPLATE_KEY)

    if reset:
        if old_value is not None:
            await _kv_delete(db, ANNOUNCE_TEMPLATE_KEY)
            await record_audit(
                db,
                action="simulation_announce_template_reset",
                actor_user_id=current_user.id,
                payload=_audit_payload(
                    {"old_hash": _hash12(old_value), "new_hash": None}, current_user
                ),
            )
        flash(request, "Template reset to file default.", "success")
        return RedirectResponse(url=_panel_url(request), status_code=302)

    error = validate_template(body)
    if error:
        ctx = await simulation_page_context(
            db, request, current_user, template_error=error, template_value_override=body
        )
        return render_simulation_page(request, ctx)

    await _kv_upsert(db, ANNOUNCE_TEMPLATE_KEY, body)
    await record_audit(
        db,
        action="simulation_announce_template_updated",
        actor_user_id=current_user.id,
        payload=_audit_payload(
            {"old_hash": _hash12(old_value), "new_hash": _hash12(body)}, current_user
        ),
    )
    flash(request, "Template saved.", "success")
    return RedirectResponse(url=_panel_url(request), status_code=302)
