"""The simulation control panel page, /workspace/simulation.

Admins only for now; managers later (owner, 2026-10-27). The page's forms post to the
/admin/simulation/* routes (src/routers/admin/simulation.py), which redirect back here,
and GET /admin/simulation redirects here too. The context skips the workspace projection
(``workspace_context``): it carries admin-only data (commands, the audit trail, spend), so
opening the page to managers needs that projection decided first, and the page's gate
and the nav item's ``admin_only`` change together; the writes stay admin-only unless that
is decided separately.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.run_marker import template_body
from src.agent.sessions import FAILURES_IN_A_ROW_MAX, SYSTEMS, describe_records
from src.config import get_settings
from src.database import get_db
from src.dependencies import get_admin_user
from src.models import AdminAuditEvent, AppSetting, SimulationCommand, SimulationRun, User
from src.services import display_format as fmt
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH
from src.services.headline_claims import held_headline_counts
from src.services.project_research import research_unavailable_reason
from src.services.runs import latest_run_id
from src.services.simulation_control import (
    SYSTEM_STARTING,
    is_finalize_stop,
    systems_view,
    takes_system_commands,
)
from src.services.simulation_control import panel_state as read_panel_state
from src.services.simulation_stats import funnel, spend_by_system
from src.services.simulation_view import live_tab_context
from src.web.page_context import page_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_ADMIN = Depends(get_admin_user)

#: The `app_settings` keys of the run-start announcement's DB overrides: the channel list
#: and the message body (src/agent/engine/run_announcer.py reads both).
ANNOUNCE_CHANNELS_KEY = "run_start_announce_channels"
ANNOUNCE_TEMPLATE_KEY = "run_start_announcement_template"

#: The engine posts at most this many owed headlines on a Stop
#: (src/agent/engine/constants.py HEADLINES_MAX_AT_SHUTDOWN). Duplicated, not
#: imported: that module imports src.agent.agent; the equality is pinned by
#: tests/unit/test_stop_announce_cap.py.
STOP_ANNOUNCE_CAP = 25


async def kv_get(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(select(AppSetting.value).where(AppSetting.key == key))
    return result.scalar_one_or_none()


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
    announced (the run was resumed) is not owed again. None when no engine holds the
    lock or the heartbeat has not named a run yet; the dialog then words it without
    numbers.
    """
    if not engine_is_alive or status_row is None or status_row.simulation_run_id is None:
        return None
    live = await funnel(db, status_row.simulation_run_id)
    owed = live.headlines_owed + live.provisional_unannounced
    return {"owed": owed, "open": live.provisional_unannounced, "cap": STOP_ANNOUNCE_CAP,
            "posts": min(owed, STOP_ANNOUNCE_CAP)}


def _with_minutes_used(systems: dict[str, dict], now: datetime) -> dict[str, dict]:
    """``systems_view`` plus ``minutes_used``: whole minutes since the session started
    (None when the heartbeat gave no start)."""
    out = {}
    for name, view in systems.items():
        started = view.get("started_at")
        minutes = int((now - started).total_seconds() // 60) if started is not None else None
        out[name] = {**view, "minutes_used": minutes}
    return out


def _status_systems(status_row) -> list[dict]:
    """The engine status card's line per system: ``describe_records`` rows of the
    heartbeat's ``detail["systems"]``, the supervisor's ``starting`` entries included."""
    systems = (status_row.detail or {}).get("systems") if status_row is not None else None
    if not isinstance(systems, dict):
        return []
    rows = []
    for name in SYSTEMS:
        record = systems.get(name)
        if not isinstance(record, dict):
            continue
        row = describe_records([{**record, "system": name}])[0]
        if record.get("state") == SYSTEM_STARTING:
            row["outcome"] = "starting"
        rows.append(row)
    return rows


async def simulation_page_context(
    db: AsyncSession,
    request: Request,
    current_user: User,
    *,
    template_error: str | None = None,
    template_value_override: str | None = None,
) -> dict:
    """Assemble every value templates/workspace/simulation.html renders.

    Shared by the GET route and by the announce-template POST's inline-error
    re-render (the one panel POST that renders the page directly instead of
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

    channels_kv = await kv_get(db, ANNOUNCE_CHANNELS_KEY)
    channels_default = get_settings().run_start_announce_channels
    channels_value = channels_kv if channels_kv is not None else channels_default

    if template_value_override is not None:
        template_value = template_value_override
    else:
        template_kv = await kv_get(db, ANNOUNCE_TEMPLATE_KEY)
        template_value = template_kv if template_kv is not None else template_body()

    audit_result = await db.execute(
        select(AdminAuditEvent).order_by(AdminAuditEvent.created_at.desc()).limit(20)
    )
    audit_events = audit_result.scalars().all()

    live_tab = await live_tab_context(db, request, status_row, now)
    stop_counts = await _stop_counts(db, status_row, engine_is_alive)

    # The Start/stop section's cards (spec §4.2 of
    # docs/specs/2026-10-27-independent-simulation-controls-design.md): the live run is
    # the one the heartbeat names while an engine holds the lock.
    live_run_id = (
        status_row.simulation_run_id
        if engine_is_alive and status_row is not None else None
    )
    live_run = await db.get(SimulationRun, live_run_id) if live_run_id is not None else None
    systems = _with_minutes_used(systems_view(status_row, engine_is_alive=engine_is_alive), now)
    system_spend = None
    if live_run_id is not None:
        spend = await spend_by_system(db, live_run_id)
        system_spend = {name: fmt.money(value) for name, value in spend.items()}
    pending_system = next((c for c in pending_commands if c.command == "system"), None)
    pending_stop = next((c for c in pending_commands if c.command == "stop"), None)
    finalize_pending = pending_stop is not None and is_finalize_stop(pending_stop)
    # What the start and system routes refuse, so the page disables those buttons rather
    # than offer a click that only flashes the refusal: a stopping engine (its heartbeat
    # says so, or a stop waits for its next control poll) and an engine that cannot take
    # `system` commands.
    run_stopping = engine_is_alive and (
        (status_row is not None and status_row.state == "stopping")
        or (pending_stop is not None and not finalize_pending)
    )
    legacy_engine = engine_is_alive and not takes_system_commands(status_row)

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

    return page_context(
        request,
        current_user,
        section="simulation",
        panel_state=panel_state,
        engine_is_alive=engine_is_alive,
        status_row=status_row,
        latest_run=latest_run,
        held_counts=held_counts,
        stop_counts=stop_counts,
        latest_finalized=latest_finalized,
        pending_commands=pending_commands,
        pending_start=pending_start,
        pending_system=pending_system,
        finalize_pending=finalize_pending,
        run_stopping=run_stopping,
        legacy_engine=legacy_engine,
        live_run_id=live_run_id,
        live_run=live_run,
        systems=systems,
        system_spend=system_spend,
        # A dead engine's last heartbeat no longer describes anything that runs; the
        # supervisor's ``starting`` lines show until its engine takes the lock.
        status_systems=(
            _status_systems(status_row)
            if engine_is_alive or panel_state == "starting" else []
        ),
        research_unavailable=research_unavailable_reason(),
        failures_in_a_row_max=FAILURES_IN_A_ROW_MAX,
        recent_commands=recent_commands,
        channels_value=channels_value,
        channels_kv=channels_kv,
        channels_default=channels_default,
        template_value=template_value,
        audit_events=audit_events,
        template_error=template_error,
        tick_at_display=tick_at_display,
        web_rubric_hash=RUBRIC_CONTENT_HASH,
        **live_tab,
    )


def render_simulation_page(request: Request, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, "workspace/simulation.html", ctx)


@router.get("/simulation", response_class=HTMLResponse, name="workspace_simulation")
async def workspace_simulation(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """The simulation control panel: status card, start/stop forms, the
    announce-channels + announce-template editors, and recent command/audit
    history, then the Live tab's stats sections — see the Live-tab comment
    block inside the template."""
    ctx = await simulation_page_context(db, request, current_user)
    return render_simulation_page(request, ctx)
