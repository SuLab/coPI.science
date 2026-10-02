"""Admin /admin/simulation control panel routes."""

import hashlib
import re
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.run_marker import parse_announce_channels, template_body, validate_template
from src.config import get_settings
from src.models import AdminAuditEvent, AppSetting, SimulationCommand, SimulationRun, User
from src.routers.admin._common import _ADMIN, _DB, _template_context, router, templates
from src.services import display_format as fmt
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH
from src.services.headline_claims import held_headline_counts
from src.services.runs import latest_run_id
from src.services.simulation_control import (
    engine_alive,
    enqueue_command,
    is_finalize_stop,
    record_audit,
)
from src.services.simulation_control import panel_state as read_panel_state
from src.services.simulation_stats import funnel
from src.services.simulation_view import live_tab_context
from src.web.flash import flash

# ---------------------------------------------------------------------------
# /admin/simulation — the control-plane panel (Task 7 of
# docs/plans/2026-08-30-simulation-control-panel.md). Command/heartbeat/audit primitives live in
# src.services.simulation_control; this module stays thin — read the panel
# state, shape a form, ask the service a question, render or redirect. The
# supervisor (src/agent/supervisor.py) and the running engine's own
# `_poll_control_plane` are the only consumers of the rows written here.
# ---------------------------------------------------------------------------

#: Slack's own channel-name shape (lowercase letters, digits, hyphens,
#: underscores, up to 80 chars) — checked per name AFTER
#: parse_announce_channels has already split/deduped/trimmed the raw input,
#: so an admin typo is caught here rather than surfacing later as a silent
#: channel_not_found from Slack at run-start time.
_ANNOUNCE_CHANNEL_RE = re.compile(r"^[a-z0-9_-]{1,80}$")


_KEY_ANNOUNCE_CHANNELS = "run_start_announce_channels"

_KEY_ANNOUNCE_TEMPLATE = "run_start_announcement_template"



def _hash12(text: str | None) -> str | None:
    """First 12 hex chars of a sha256 digest, or None for a None input.

    The ONLY form the announce-template audit payload ever records of a
    template body — never the full text (it may echo an unpublished PI
    disclosure's placeholder shape, or simply be long).
    """
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]



async def _kv_get(db: AsyncSession, key: str) -> str | None:
    return (await db.execute(select(AppSetting.value).where(AppSetting.key == key))).scalar_one_or_none()



async def _kv_upsert(db: AsyncSession, key: str, value: str) -> None:
    stmt = pg_insert(AppSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
    await db.execute(stmt)



async def _kv_delete(db: AsyncSession, key: str) -> None:
    await db.execute(sa_delete(AppSetting).where(AppSetting.key == key))



#: The engine posts at most this many owed headlines on a Stop
#: (src/agent/engine/constants.py HEADLINES_MAX_AT_SHUTDOWN). Duplicated, not
#: imported: that module imports src.agent.agent; the equality is pinned by
#: tests/unit/test_stop_announce_cap.py.
STOP_ANNOUNCE_CAP = 25


async def _stop_counts(
    db: AsyncSession, status_row, engine_is_alive: bool
) -> dict[str, int] | None:
    """The announcing Stop's dialog numbers, from the LIVE run's funnel (FN-01).

    The page's Live tab shows the ``?run=`` selection, which defaults to the newest run
    by ``started_at`` and need not be the one the engine is executing; the heartbeat
    row names the live run. A Stop announces every owed headline of that run, open
    interviews included: ``owed`` is the terminal ones owed plus the provisional
    (still open) ones not yet announced, ``open`` those provisional ones, ``posts``
    what one Stop posts (at most ``cap``). An open interview an earlier Stop already
    announced (the run was resumed) is not owed again. None when no engine holds the lock or the heartbeat has not
    named a run yet; the dialog then words it without numbers.
    """
    if not engine_is_alive or status_row is None or status_row.simulation_run_id is None:
        return None
    live = await funnel(db, status_row.simulation_run_id)
    owed = live.headlines_owed + live.provisional_unannounced
    return {"owed": owed, "open": live.provisional_unannounced, "cap": STOP_ANNOUNCE_CAP,
            "posts": min(owed, STOP_ANNOUNCE_CAP)}


async def _simulation_context(
    db: AsyncSession,
    request: Request,
    current_user: User,
    *,
    msg: str | None = None,
    template_error: str | None = None,
    template_value_override: str | None = None,
) -> dict:
    """Assemble every value templates/admin/simulation.html renders.

    Shared by the GET route and by the announce-template POST's inline-error
    re-render (the one POST here that renders the page directly instead of
    redirecting — see that handler's docstring for why).
    """
    now = datetime.now(UTC)
    panel_state, status_row, engine_is_alive = await read_panel_state(db, now)

    pending_result = await db.execute(
        select(SimulationCommand)
        .where(SimulationCommand.status == "pending")
        .order_by(SimulationCommand.created_at)
    )
    pending_commands = pending_result.scalars().all()
    pending_start = next((c for c in pending_commands if c.command == "start"), None)

    recent_result = await db.execute(
        select(SimulationCommand).order_by(SimulationCommand.created_at.desc()).limit(20)
    )
    recent_commands = recent_result.scalars().all()

    latest_id = await latest_run_id(db)
    latest_run = await db.get(SimulationRun, latest_id) if latest_id is not None else None
    # Held headlines of a stopped run (spec §8.2): owed, never claimed.
    held_counts = None
    if latest_run is not None and latest_run.status == "stopped":
        held_counts = await held_headline_counts(db, latest_run.id)
    latest_finalized = latest_run is not None and latest_run.finalized_at is not None

    channels_kv = await _kv_get(db, _KEY_ANNOUNCE_CHANNELS)
    channels_default = get_settings().run_start_announce_channels
    channels_value = channels_kv if channels_kv is not None else channels_default

    if template_value_override is not None:
        template_value = template_value_override
    else:
        template_kv = await _kv_get(db, _KEY_ANNOUNCE_TEMPLATE)
        template_value = template_kv if template_kv is not None else template_body()

    audit_result = await db.execute(
        select(AdminAuditEvent).order_by(AdminAuditEvent.created_at.desc()).limit(20)
    )
    audit_events = audit_result.scalars().all()

    live_tab = await live_tab_context(db, request, status_row, now)
    stop_counts = await _stop_counts(db, status_row, engine_is_alive)

    # The heartbeat's `tick_at` is an ISO string with microseconds; every other
    # time on this page is minute-precision UTC. An unparseable value is shown
    # verbatim rather than hidden — it is then the only clue it is malformed.
    raw_tick_at = (status_row.detail or {}).get("tick_at") if status_row is not None else None
    if isinstance(raw_tick_at, str):
        try:
            tick_at_display = fmt.timestamp(datetime.fromisoformat(raw_tick_at))
        except ValueError:
            tick_at_display = raw_tick_at
    else:
        tick_at_display = "—"

    return _template_context(
        request,
        current_user,
        active_admin="simulation",
        panel_state=panel_state,
        engine_is_alive=engine_is_alive,
        status_row=status_row,
        latest_run=latest_run,
        held_counts=held_counts,
        stop_counts=stop_counts,
        latest_finalized=latest_finalized,
        pending_commands=pending_commands,
        pending_start=pending_start,
        recent_commands=recent_commands,
        channels_value=channels_value,
        channels_kv=channels_kv,
        channels_default=channels_default,
        template_value=template_value,
        audit_events=audit_events,
        msg=msg,
        template_error=template_error,
        tick_at_display=tick_at_display,
        web_rubric_hash=RUBRIC_CONTENT_HASH,
        **live_tab,
    )



def _refuse_to(request: Request, url: str, message: str) -> RedirectResponse:
    """Flash ``message`` as an error and redirect to ``url`` (A-14: never in the query)."""
    flash(request, message, "error")
    return RedirectResponse(url=url, status_code=302)


@router.get("/simulation", response_class=HTMLResponse)
async def admin_simulation(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """The simulation control panel: status card, start/stop forms, the
    announce-channels + announce-template editors, and recent command/audit
    history, then the Live tab's stats sections — see the Live-tab comment
    block inside the template."""
    ctx = await _simulation_context(
        db,
        request,
        current_user,
        msg=request.query_params.get("msg"),
    )
    return templates.TemplateResponse(request, "admin/simulation.html", ctx)



@router.post("/simulation/start")
async def admin_simulation_start(
    request: Request,
    fresh: bool = Form(False),
    max_runtime: int = Form(0),
    max_proposals: int = Form(0),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Enqueue a `start` command for the supervisor to claim.

    Refused (no row written) while an engine holds the engine lock
    (`engine_alive`, spec §8.3) — CLI-launched or panel-launched alike — or
    while a `start` is already pending (the common double-click case, caught here before
    ever reaching the database). The 0042 partial unique index
    (`uq_simulation_commands_one_pending`) is the second, race-proof layer:
    an `IntegrityError` from `enqueue_command` is caught and rendered as the
    same refusal. DECLARED v1 decision: this two-field form plus these
    refusals ARE the confirmation step — no JS confirm dialog — and the
    sharp CLI-only flags (`--all-agents`/`--reset-cursors`) are deliberately
    not exposed here; the page links no substitute.
    """
    alive = await engine_alive(db)
    pending_start = (
        await db.execute(
            select(SimulationCommand).where(
                SimulationCommand.status == "pending",
                SimulationCommand.command == "start",
            )
        )
    ).scalar_one_or_none()
    if alive or pending_start is not None:
        return _refuse_to(
            request, "/admin/simulation", "A run is already starting or in progress."
        )
    pending_stop = (
        await db.execute(
            select(SimulationCommand).where(
                SimulationCommand.status == "pending",
                SimulationCommand.command == "stop",
            )
        )
    ).scalar_one_or_none()
    if pending_stop is not None and is_finalize_stop(pending_stop):
        # The finalize would stamp the run a start is about to resume or replace.
        return _refuse_to(
            request, "/admin/simulation", "A Finalize run is pending; start after it finishes."
        )
    latest_id = await latest_run_id(db)
    latest = await db.get(SimulationRun, latest_id) if latest_id is not None else None
    if latest is not None and latest.finalized_at is not None:
        # A finalized run refuses a resume (RunFinalized); the form forces Fresh
        # and so does the route, so the operator never queues a doomed start.
        fresh = True
    payload = {"fresh": fresh, "max_runtime": max_runtime, "max_proposals": max_proposals}
    try:
        await enqueue_command(
            db, command="start", payload=payload, requested_by_user_id=current_user.id
        )
    except IntegrityError:
        await db.rollback()
        return _refuse_to(request, "/admin/simulation", "A start is already pending.")
    await record_audit(
        db, action="simulation_start_requested", actor_user_id=current_user.id, payload=payload
    )
    return RedirectResponse(
        url=f"/admin/simulation?msg={quote('Start requested.')}", status_code=302
    )


_RUN_ID_FORM = Form(...)


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
    first 8 characters (case and surrounding spaces ignored)."""
    def _refuse(message: str):
        return _refuse_to(request, f"/admin/activity/{run_id}", message)

    short_id = str(run_id)[:8]
    if confirm_run.strip().lower() != short_id:
        # C-10: the run's short id, typed, is the confirmation; checked here, not only
        # in the browser's dialog.
        return _refuse(f"Type the run's short id ({short_id}) to confirm Finalize run.")

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
    run = await db.get(SimulationRun, run_id)
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
        db, action="simulation_finalize_requested", actor_user_id=current_user.id, payload=payload,
    )
    return RedirectResponse(
        url=f"/admin/activity/{run_id}?msg={quote('Finalize run requested.')}", status_code=302,
    )


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
        return _refuse_to(request, "/admin/simulation", "Nothing is running.")
    payload = {"hold_open": True} if hold_open == "1" else None
    try:
        await enqueue_command(
            db, command="stop", payload=payload, requested_by_user_id=current_user.id,
        )
    except IntegrityError:
        await db.rollback()
        return _refuse_to(request, "/admin/simulation", "A stop is already pending.")
    await record_audit(
        db, action="simulation_stop_requested", actor_user_id=current_user.id, payload=payload
    )
    message = "Stop (hold open interviews) requested." if payload else "Stop requested."
    return RedirectResponse(url=f"/admin/simulation?msg={quote(message)}", status_code=302)



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
    (`_simulation_context`'s `channels_kv`): an explicit `disable` writes the
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
    old_value = await _kv_get(db, _KEY_ANNOUNCE_CHANNELS)

    if disable:
        new_value = ""
        await _kv_upsert(db, _KEY_ANNOUNCE_CHANNELS, new_value)
        msg = "Announcements disabled."
    else:
        names = parse_announce_channels(channels)
        bad = [n for n in names if not _ANNOUNCE_CHANNEL_RE.match(n)]
        if bad:
            return _refuse_to(
                request, "/admin/simulation", "Invalid channel name(s): " + ", ".join(bad)
            )
        new_value = ",".join(names) or None
        if new_value is None:
            await _kv_delete(db, _KEY_ANNOUNCE_CHANNELS)
        else:
            await _kv_upsert(db, _KEY_ANNOUNCE_CHANNELS, new_value)
        msg = "Announce channels updated."

    await record_audit(
        db,
        action="simulation_announce_channels_updated",
        actor_user_id=current_user.id,
        payload={"old": old_value, "new": new_value},
    )
    return RedirectResponse(url=f"/admin/simulation?msg={quote(msg)}", status_code=302)



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
    old_value = await _kv_get(db, _KEY_ANNOUNCE_TEMPLATE)

    if reset:
        if old_value is not None:
            await _kv_delete(db, _KEY_ANNOUNCE_TEMPLATE)
            await record_audit(
                db,
                action="simulation_announce_template_reset",
                actor_user_id=current_user.id,
                payload={"old_hash": _hash12(old_value), "new_hash": None},
            )
        return RedirectResponse(
            url=f"/admin/simulation?msg={quote('Template reset to file default.')}",
            status_code=302,
        )

    error = validate_template(body)
    if error:
        ctx = await _simulation_context(
            db, request, current_user, template_error=error, template_value_override=body
        )
        return templates.TemplateResponse(request, "admin/simulation.html", ctx)

    await _kv_upsert(db, _KEY_ANNOUNCE_TEMPLATE, body)
    await record_audit(
        db,
        action="simulation_announce_template_updated",
        actor_user_id=current_user.id,
        payload={"old_hash": _hash12(old_value), "new_hash": _hash12(body)},
    )
    return RedirectResponse(url=f"/admin/simulation?msg={quote('Template saved.')}", status_code=302)
