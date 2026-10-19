"""Run-selection queries shared by the web routes."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import SimulationProcessStatus, SimulationRun
from src.services.simulation_control import HEARTBEAT_STALE_SECONDS


async def latest_run_id(db: AsyncSession) -> uuid.UUID | None:
    """The newest simulation run's id, or None when there are no runs.

    Ordered by ``started_at DESC, id DESC`` so two runs that share a
    ``started_at`` resolve the same way on every call: ``started_at`` alone is
    not a total order, and ``LIMIT 1`` over a tie may return either row.
    """
    return (
        await db.execute(
            select(SimulationRun.id)
            .order_by(SimulationRun.started_at.desc(), SimulationRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def runs_ordered(db: AsyncSession) -> list[SimulationRun]:
    """Every run, newest first; ties on started_at broken by id, the same order
    ``latest_run_id`` picks from."""
    return list(
        (
            await db.execute(
                select(SimulationRun).order_by(
                    SimulationRun.started_at.desc(), SimulationRun.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )


async def live_run_id(db: AsyncSession, *, now: datetime | None = None) -> uuid.UUID | None:
    """The run the engine is running right now, or None.

    The ``simulation_process_status`` row (id 1) names the engine's run; it counts as
    live while its ``state`` is ``running`` or ``stopping`` and its heartbeat
    (``updated_at``) is no older than ``HEARTBEAT_STALE_SECONDS`` — the heartbeat half of
    ``/admin/simulation``'s rule, without its engine-lock probe, so an engine that died
    keeps **Live** until its heartbeat goes stale. A stored ``status == "running"`` alone is not enough:
    three July-August rows kept it after their engines died.
    """
    row = await db.get(SimulationProcessStatus, 1)
    if row is None or row.state not in ("running", "stopping"):
        return None
    moment = now or datetime.now(UTC)
    if (moment - row.updated_at).total_seconds() > HEARTBEAT_STALE_SECONDS:
        return None
    return row.simulation_run_id


def run_bar_entries(
    runs: list[SimulationRun], counts: Mapping[uuid.UUID, int], *, live_id: uuid.UUID | None
) -> list[dict]:
    """One plain dict per run, in ``runs`` order, for ``templates/_run_bar.html``.

    Keys: ``id``, ``started_at``, ``ended_at``, ``rubric_version`` (``config`` value when
    a str, else None), ``count`` (``counts.get(id, 0)``), ``is_current`` (``runs[0]``),
    ``is_live`` (``id == live_id``) and ``badge``: ``"Live"`` when live, else
    ``"Finalized"`` when ``finalized_at`` is set, else ``"Held"`` when ``held_at`` is set,
    else ``status.capitalize()``. Plain dicts, so reviewers see the badge without the
    run's other configuration crossing the page projection.
    """
    entries = []
    for index, run in enumerate(runs):
        version = (run.config or {}).get("rubric_version")
        if run.id == live_id:
            badge = "Live"
        elif run.finalized_at:
            badge = "Finalized"
        elif run.held_at:
            badge = "Held"
        else:
            badge = run.status.capitalize()
        entries.append(
            {
                "id": run.id,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "rubric_version": version if isinstance(version, str) else None,
                "count": counts.get(run.id, 0),
                "is_current": index == 0,
                "is_live": run.id == live_id,
                "badge": badge,
            }
        )
    return entries
