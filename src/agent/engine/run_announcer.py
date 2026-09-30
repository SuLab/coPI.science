"""The run-start marker: its values, the admin overrides, the post and its record (spec §7.1)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from typing import TYPE_CHECKING

from src.agent.engine import deps
from src.agent.engine.context import EngineContext, via
from src.agent.roles import prompt_set_stamp
from src.agent.run_marker import (
    parse_announce_channels,
    render_run_start_announcement,
)
from src.models import SimulationRun
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory
    from src.agent.engine.scheduler import Scheduler
    from src.agent.engine.slack_io import SlackIO

logger = logging.getLogger("src.agent.simulation")


class RunAnnouncer:
    """The run-start marker posted to the announce channels on a fresh run."""

    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    slack_clients = via("ctx")
    _channel_id_map = via("_channel_directory")
    _start_time = via("_scheduler")
    max_runtime_minutes = via("_scheduler")
    _next_poll_client = via("_slack_io")

    OWNED_STATE: tuple[str, ...] = ()

    def __init__(
        self,
        ctx: EngineContext,
        *,
        slack_io: SlackIO,
        channel_directory: ChannelDirectory,
        scheduler: Scheduler,
    ) -> None:
        self.ctx = ctx
        self._slack_io = slack_io
        self._channel_directory = channel_directory
        self._scheduler = scheduler

    def _run_start_announcement_values(self) -> dict[str, str]:
        """The 12 template placeholders (run_marker.ANNOUNCEMENT_VALUE_KEYS).

        Every value is a plain pre-rendered string so an operator template
        needs no format specs. The git identity describes the IMAGE this
        process runs from (see src/services/build_info.py) — for the agent
        that is exactly the code executing, since src/ is baked at build.
        """
        started = self._start_time or deps.datetime.now(UTC)
        build = deps.get_build_info()
        hub_stamp = prompt_set_stamp("scout_hub")
        pi_stamp = prompt_set_stamp("pi_lab")
        if build.dirty_files is None:
            dirty = "dirty state unknown"
        elif build.dirty_files == 0:
            dirty = "clean"
        else:
            dirty = f"{build.dirty_files} uncommitted change(s) at image build"
        return {
            "run_id": str(self.simulation_run_id) if self.simulation_run_id
            else "unrecorded (--no-db)",
            "started_at": started.strftime("%Y-%m-%d %H:%M UTC"),
            "run_duration": (
                f"{self.max_runtime_minutes} minutes"
                if self.max_runtime_minutes > 0 else "indefinite (until stopped)"
            ),
            "git_commit": build.commit[:7] if build.commit else "unknown",
            "git_branch": build.branch or "unknown",
            "git_dirty": dirty,
            "hub_prompts_version": hub_stamp.version,
            "hub_prompts_hash": hub_stamp.content_hash,
            "pi_prompts_version": pi_stamp.version,
            "pi_prompts_hash": pi_stamp.content_hash,
            "rubric_version": RUBRIC_VERSION,
            "rubric_hash": RUBRIC_CONTENT_HASH,
        }

    async def _announce_overrides(self) -> tuple[str | None, str | None]:
        """Read the two DB-overridable announce settings in one session.

        Returns ``(channels, template_body)``: each is ``None`` when its
        ``app_settings`` row is absent or its value is NULL — the caller
        reads that as "no override, fall back to Settings / the template
        file". Only called by ``_announce_run_start`` when
        ``self.session_factory`` is set. This has its OWN try/except, distinct
        from ``_announce_run_start``'s outer one: a KV hiccup here must cost
        the announcement at most a fallback, not the channel-resolution and
        posting work still to come in that method — the same reasoning as
        ``_poll_control_plane``'s own try/except.
        """
        try:
            from sqlalchemy import select as sa_select

            from src.models import AppSetting

            keys = (
                "run_start_announce_channels",
                "run_start_announcement_template",
            )
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(AppSetting.key, AppSetting.value).where(
                        AppSetting.key.in_(keys)
                    )
                )).all()
            values = dict(rows)
            return (
                values.get("run_start_announce_channels"),
                values.get("run_start_announcement_template"),
            )
        except Exception as exc:
            logger.warning(
                "Run-start announcement: app_settings read failed (%s) — "
                "falling back to Settings/template-file defaults",
                exc,
            )
            return None, None

    async def _announce_run_start(self) -> None:
        """Post the run-start marker to every configured channel (fresh runs
        only — the caller gates on self._fresh_start).

        Best-effort end to end, same philosophy as _post_assessment_summary:
        nothing here may take down a run start. Refusals arrive as a falsy
        return from post_message, not as exceptions, so the return value is
        checked per channel. Posts with the hub's client (the engine's voice,
        and the identity with zero blast radius if the ingest sentinel ever
        regressed — see run_marker.py); falls back to any connected client
        with a WARNING. The markers post AFTER the fresh-start cursor seed,
        so the live poller WILL fetch them on its first tick — the sentinel
        skip (Task 4 of docs/plans/2026-08-29-run-start-announcements.md) is
        what drops them there and on every later resume.
        """
        try:
            names = parse_announce_channels(
                deps.get_settings().run_start_announce_channels
            )
            template_override: str | None = None
            if self.session_factory:
                channels_override, template_override = await self._announce_overrides()
                if channels_override is not None:
                    names = parse_announce_channels(channels_override)
            if not names:
                logger.info("Run-start announcement disabled (no channels configured)")
                return

            hub = next(
                (a for a in self.agents.values() if a.role == "scout_hub"), None
            )
            client = self.slack_clients.get(hub.agent_id) if hub else None
            if not client or not client.is_connected:
                fallback = self._next_poll_client()
                if fallback is None:
                    logger.info(
                        "Run-start announcement skipped: no connected Slack "
                        "client (Slack off or all tokens dead)"
                    )
                    return
                logger.warning(
                    "Run-start announcement: hub client unavailable — "
                    "falling back to [%s]'s client (marker will carry a "
                    "lab bot's identity)", fallback.agent_id,
                )
                client = fallback

            text = render_run_start_announcement(
                self._run_start_announcement_values(), template_override
            )
            posted: dict[str, str] = {}
            failed: list[str] = []
            try:
                for name in names:
                    # Both id resolution and the post itself sit inside this
                    # one per-channel try: a non-SlackApiError exception
                    # during get_channel_id (a network blip, say) must skip
                    # only THIS channel, not abort every channel still to
                    # come. ch_id stays None if resolution itself raised, so
                    # the "raised" WARNING below still has something to log.
                    ch_id = None
                    try:
                        ch_id = self._channel_id_map.get(name)
                        if not ch_id:
                            ch_id = await asyncio.to_thread(client.get_channel_id, name)
                        if not ch_id or ch_id.startswith("local:"):
                            logger.warning(
                                "Run-start announcement: cannot resolve #%s to "
                                "a real Slack channel (got %r) — skipping it",
                                name, ch_id,
                            )
                            failed.append(name)
                            continue
                        result = await client.apost_message(ch_id, text)
                    except Exception:  # noqa: BLE001 — per-channel isolation
                        logger.warning(
                            "Run-start announcement to #%s (%s) raised — skipping",
                            name, ch_id, exc_info=True,
                        )
                        failed.append(name)
                        continue
                    ts = (result or {}).get("ts")
                    if ts:
                        posted[name] = ts
                    else:
                        logger.warning(
                            "Run-start announcement to #%s (%s) was refused by "
                            "Slack — nothing posted there", name, ch_id,
                        )
                        failed.append(name)
            finally:
                # Runs even on a mid-loop escape (nothing above should escape
                # the per-channel try, but this is the recorded contract, not
                # a hope): whatever actually posted before any such escape is
                # still logged and written to the run row.
                logger.info(
                    "Run-start announcement: posted to %d channel(s)%s — %s",
                    len(posted),
                    f", {len(failed)} failed ({', '.join(failed)})" if failed else "",
                    ", ".join(posted) or "none",
                )
                await self._record_run_start_announcement(text, posted, failed)
        except Exception:
            logger.exception("Run-start announcement failed — continuing startup")

    async def _record_run_start_announcement(
        self, text: str, posted: dict[str, str], failed: list[str],
    ) -> None:
        """Durably record what was announced on the run row's config.

        Reassigns the whole dict rather than mutating: SimulationRun.config is
        a plain JSON column (src/models/agent_activity.py:65) with no mutation
        tracking, so an in-place update would silently not persist. Best-effort
        and never raises — the Slack posts already happened.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select
        try:
            async with self.session_factory() as db:
                run = (await db.execute(
                    sa_select(SimulationRun).where(
                        SimulationRun.id == self.simulation_run_id
                    )
                )).scalar_one_or_none()
                if run is None:
                    return
                run.config = {
                    **(run.config or {}),
                    "run_start_announcement": {
                        "at": deps.datetime.now(UTC).isoformat(),
                        "text": text,
                        "posted": posted,
                        "failed": failed,
                    },
                }
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — record is advisory
            logger.warning(
                "Could not record the run-start announcement on run %s: %s",
                self.simulation_run_id, exc,
            )
