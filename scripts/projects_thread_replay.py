"""Offline replay of one BlackbirdBot project thread on a scratch database (the projects
prototype design, §8).

Seeds a SCRATCH database with BlackbirdBot, one lab, a run, its clock and one
``ready_to_post`` project taken from a ``scripts/projects_dry_run.py`` output file, then runs
the real engine with Slack off until the project finishes, and prints the posting, the
thread and the outcome. ``--max-minutes`` winds the Projects session down: nothing new is
requested, but a project already researched runs to its end, so the engine may run past it.
Nothing reaches Slack, but the model calls are real and billed: the owner approves each use.

The lab's persona comes from the mounted ``profiles/`` (refused when the lab's public file is
missing). The hub has none by design and runs on ``Agent.public_profile``'s fallback, as in
production. The engine runs one Projects session (no Interviews session, so the lab never
pitches its own idea, no interview starts and no working-memory file is written under the live
``profiles/``) limited to ``--max-minutes``, and every engine read of the settings sees
``replay_settings_overrides()``: cohort isolation off (only the hub and one lab run) and
``lab_daily_post_cap=0``. The lab may be requested a second project once the first finishes;
nothing researches it, and the replay reports only the seeded one.

Refused: a ``--db-url`` whose database is ``copi``; a process ``DATABASE_URL`` that names a
different database (so nothing in the process can reach another one); a ``--lab`` that is
not in the file or whose ``outcome`` is not ``ready_to_post``.

On the host, against a scratch Postgres on this stack's network (never the other
deployment's containers), migrated to head first, with
``R=postgresql+asyncpg://copi:replay@copi-blackbird-replay-pg:5432/copi_replay``:

  $DC --profile agent run --rm --no-deps -T --name copi-blackbird-projects-replay \\
      -e DATABASE_URL=$R -v ~/backups-blackbird/projects-dry-run:/out agent \\
      python scripts/projects_thread_replay.py --db-url $R \\
      --dry-run-json /out/<date>.json --lab <agent_id>

Exit 0 when the project finished, 1 when the engine stopped before it did, 2 on a refusal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from src.agent import agent as agent_module  # noqa: E402
from src.agent.agent import Agent  # noqa: E402
from src.agent.engine import deps  # noqa: E402
from src.agent.role_capabilities import (  # noqa: E402
    hub_role_names,
    roles_requiring_user,
    spoke_role_names,
)
from src.agent.sessions import PROJECTS, Limits, Sessions  # noqa: E402
from src.agent.simulation import SimulationEngine  # noqa: E402
from src.agent.transport import NullTransport  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.models import AgentMessage, AgentRegistry, SimulationRun, User  # noqa: E402
from src.models.hub_project import (  # noqa: E402
    FINISHED_STATUSES,
    STATUS_READY_TO_POST,
    HubProject,
    HubProjectClock,
)
from src.services.agent_identity import derive_agent_identity  # noqa: E402

HUB_BOT_NAME = "BlackbirdBot"
HUB_PI_NAME = "Blackbird"
PRODUCTION_DATABASE = "copi"
POLL_SECONDS = 15.0


class ReplayRefused(Exception):
    """The replay will not run against this target or entry."""


def replay_settings_overrides() -> dict:
    """What every engine read of the settings sees during a replay (module docstring)."""
    return {
        "cohort_isolation_enabled": False,
        "lab_daily_post_cap": 0,
    }


def install_replay_settings() -> None:
    """Route the engine's settings seam through ``replay_settings_overrides()``."""
    def _replay_settings():
        return get_settings().model_copy(update=replay_settings_overrides())

    deps.get_settings = _replay_settings


def check_target(db_url: str, process_url: str | None) -> None:
    """Refuse the production database, and a process ``DATABASE_URL`` that names any other
    database than ``db_url`` (host, port and database name must agree)."""
    target = make_url(db_url)
    if target.database == PRODUCTION_DATABASE:
        raise ReplayRefused(f"--db-url names the {PRODUCTION_DATABASE!r} database; use a scratch one")
    if not process_url:
        raise ReplayRefused("DATABASE_URL is unset; set it to the same scratch database as --db-url")
    process = make_url(process_url)
    if (process.host, process.port, process.database) != (target.host, target.port, target.database):
        raise ReplayRefused("DATABASE_URL names a different database than --db-url")


def select_entry(dry_run: Mapping, lab: str) -> Mapping:
    """The dry-run file's ``labs[]`` entry for ``lab``; refused unless it is ready to post."""
    for entry in dry_run.get("labs") or []:
        if entry.get("agent_id") == lab:
            if entry.get("outcome") != STATUS_READY_TO_POST:
                raise ReplayRefused(
                    f"lab {lab!r} has outcome {entry.get('outcome')!r}, not {STATUS_READY_TO_POST!r}"
                )
            if not entry.get("posting_text") or not isinstance(entry.get("choice"), dict):
                raise ReplayRefused(f"lab {lab!r} has no stored choice or posting text")
            return entry
    raise ReplayRefused(f"lab {lab!r} is not in the dry-run file")


async def seed_replay(
    session_factory, entry: Mapping, *, hub_id: str, lab_id: str, run_id: uuid.UUID,
) -> None:
    """Write the hub and lab registry rows (active; the lab's ``users`` row when its role
    requires one), the run, its clock and the ``ready_to_post`` project from ``entry``."""
    hub_role, lab_role = hub_role_names()[0], spoke_role_names()[0]
    async with session_factory() as db:
        lab_bot_name = (await derive_agent_identity(db, entry["pi_name"]))[1]
        user_id = None
        if lab_role in roles_requiring_user():
            user = User(name=entry["pi_name"], orcid=f"replay-{uuid.uuid4().hex[:16]}")
            db.add(user)
            await db.flush()
            user_id = user.id
        db.add(AgentRegistry(
            agent_id=hub_id, bot_name=HUB_BOT_NAME, pi_name=HUB_PI_NAME,
            status="active", role=hub_role,
        ))
        db.add(AgentRegistry(
            agent_id=lab_id, bot_name=lab_bot_name, pi_name=entry["pi_name"],
            status="active", role=lab_role, user_id=user_id,
        ))
        db.add(SimulationRun(id=run_id))
        await db.flush()
        db.add(HubProjectClock(simulation_run_id=run_id, run_seconds=0.0))
        db.add(HubProject(
            simulation_run_id=run_id, agent_id=lab_id, slot=1, status=STATUS_READY_TO_POST,
            requested_run_seconds=0.0, choice=dict(entry["choice"]), title=entry.get("title"),
            posting_text=entry["posting_text"],
        ))
        await db.commit()


async def replay_agents(session_factory, hub_id: str, lab_id: str) -> list[Agent]:
    """The seeded agents, built from their registry rows as ``_load_roster`` builds them."""
    async with session_factory() as db:
        rows = (await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.bot_name, AgentRegistry.pi_name,
                   AgentRegistry.role)
            .where(AgentRegistry.agent_id.in_([hub_id, lab_id]))
            .order_by(AgentRegistry.agent_id)
        )).all()
    return [Agent(agent_id=r.agent_id, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)
            for r in rows]


def build_replay_engine(
    session_factory, agents: list[Agent], run_id: uuid.UUID, *, max_minutes: int,
) -> SimulationEngine:
    """The engine with Slack off and one Projects session limited to ``max_minutes``: a
    ``NullTransport`` per agent (a hub subscribes to a channel only through its transport's
    join, so an empty client map would leave it deaf to the lab's reply) and
    ``fresh_start=False``, so ``start()`` runs the hub's channel discovery before the first
    turn, which the lab's reply could otherwise precede."""
    sessions = Sessions()
    sessions.start(
        PROJECTS, Limits(runtime_minutes=max_minutes), datetime.now(UTC), initial=True,
    )
    return SimulationEngine(
        agents=agents,
        slack_clients={a.agent_id: NullTransport(agent_id=a.agent_id) for a in agents},
        sessions=sessions,
        max_runtime_minutes=max_minutes,
        session_factory=session_factory,
        simulation_run_id=run_id,
        slack_enabled=False,
        fresh_start=False,
    )


async def _project_row(session_factory, run_id: uuid.UUID):
    """The seeded project (slot 1)."""
    async with session_factory() as db:
        return (await db.execute(
            select(HubProject).where(HubProject.simulation_run_id == run_id, HubProject.slot == 1)
        )).scalar_one()


async def _watch(sim: SimulationEngine, session_factory, run_id: uuid.UUID) -> None:
    """Stop the engine once the project has finished."""
    while not sim.run_state.stop_event.is_set():
        await asyncio.sleep(POLL_SECONDS)
        if (await _project_row(session_factory, run_id)).status in FINISHED_STATUSES:
            sim.request_stop()
            return


async def _report(session_factory, run_id: uuid.UUID) -> bool:
    """Print the posting, the thread and the outcome; True when the project finished."""
    project = await _project_row(session_factory, run_id)
    print("=== Posting ===")
    print(project.posting_text or "")
    print("\n=== Thread ===")
    if project.reply_ts:
        async with session_factory() as db:
            rows = (await db.execute(
                select(AgentMessage).where(
                    AgentMessage.simulation_run_id == run_id,
                    (AgentMessage.message_ts == project.reply_ts)
                    | (AgentMessage.thread_ts == project.reply_ts),
                ).order_by(AgentMessage.posted_at)
            )).scalars().all()
        for row in rows:
            print(f"--- {row.sender_name} ({row.message_ts})\n{row.content}\n")
    else:
        print("(the lab did not reply)")
    print("=== Outcome ===")
    print(json.dumps({
        "status": project.status, "reason_code": project.reason_code, "final": project.final,
    }, indent=2, default=str))
    return project.status in FINISHED_STATUSES


async def _run(args: argparse.Namespace) -> int:
    from src.agent.prompt_snapshot import PromptSnapshot
    from src.agent.prompt_snapshot import install as install_prompt_snapshot
    from src.database import make_engine

    check_target(args.db_url, os.environ.get("DATABASE_URL"))
    entry = select_entry(json.loads(Path(args.dry_run_json).read_text(encoding="utf-8")), args.lab)
    persona = agent_module.PROFILES_DIR / "public" / f"{args.lab}.md"
    if not persona.is_file():
        raise ReplayRefused(f"the lab's public persona {persona} is missing")

    install_replay_settings()
    install_prompt_snapshot(PromptSnapshot.load())
    db_engine = make_engine("cli", args.db_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    try:
        await seed_replay(session_factory, entry, hub_id=args.hub, lab_id=args.lab, run_id=run_id)
        agents = await replay_agents(session_factory, args.hub, args.lab)
        sim = build_replay_engine(session_factory, agents, run_id, max_minutes=args.max_minutes)
        watcher = asyncio.create_task(_watch(sim, session_factory, run_id))
        try:
            await sim.start()
        finally:
            watcher.cancel()
            await sim.stop()
        return 0 if await _report(session_factory, run_id) else 1
    finally:
        install_prompt_snapshot(None)
        await db_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db-url", required=True, help="a migrated SCRATCH database")
    parser.add_argument("--dry-run-json", required=True, help="scripts/projects_dry_run.py output")
    parser.add_argument("--lab", required=True, help="the labs[] entry's agent_id")
    parser.add_argument("--hub", default="blackbird", help="the hub's agent_id (default blackbird)")
    parser.add_argument("--max-minutes", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ReplayRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
