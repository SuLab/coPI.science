"""T13 — the whole system, running: real LLM turns and real Slack mirroring together.

No other test runs `real_llm` and `live_slack` at once. Everything in the cohort tier
drives the engine with `NullTransport` (so a mirror that silently no-ops looks identical
from inside our own database — Rule S2), and everything in the Slack tier drives the
mirror with hand-written message text (so a scheduler or a prompt that never produces a
conversation looks identical from inside Slack). This module runs `SimulationEngine.start()`
— the real main loop, the real pollers, the real cohort gate, real Opus/Sonnet turns and
the real workspace — and then asks the one question the DB-primary design exists to answer:

    is there a message in one store that is not in the other?

Four disciplines are inherited verbatim from `test_cohort_scenarios.py`, and each of them
cost a rewrite there:

1. **The labs are complementary.** Every pair is a plausible collaboration, so nothing
   the agents fail to do can be explained away by scientific irrelevance.
2. **The roster is trimmed to the agents under test**, and the workspace is collapsed to
   ONE channel. Phase 1 keyword-matches profiles against seven seeded channels and Phase 5
   posts into whichever subscribed channel the model names; left alone, three agents
   scatter and never meet, and every outcome claim comes back inconclusive.
3. **Harness-authored messages are recorded and excluded** from every "the agents
   conversed" measurement. Counting them makes the claim true by construction.
4. **A run that produced no conversation is INCONCLUSIVE, not passing.** Said so in the
   assertion message, because at 3am the difference matters.

Two more are specific to running both dependencies at once:

5. **Channel discovery is not stubbed.** `AgentSlackClient.list_channels` used to read one
   200-item page of 500+ conversations, ordered by channel id, which is not monotonic in
   creation time — so asking Slack "does this channel exist" was a coin flip and
   `_ensure_seeded_channels` would try to *create* the probe channel it had just failed to
   see. Every client's `list_channels` was therefore replaced here with a fully paginated
   stand-in. It paginates for itself now, and the stub is gone: the whole-system test is
   the one place the engine's real bootstrap path can be observed, and a patch over the
   function under test would make that impossible. The paginator is pinned directly by
   `test_slack_client_live.py::test_list_channels_returns_every_public_channel` and, at
   the engine level, by `test_slack_lifecycle_live.py::
   test_ensure_seeded_channels_adopts_a_channel_beyond_the_first_page`.
6. **Every outbound post is guarded to the probe channel.** `_phase5_new_post` reads
   `action_data.get("channel", "general")` and posts there without checking it against the
   target post's channel, so one malformed JSON reply from a model would write into the
   workspace's real `#general`. The guard raises instead, and the count is asserted to be
   zero — a violation is a finding, not a flake.

The near-concluded seeded thread is a **precondition, not the claim**. Measured over 16
real turns in the cohort tier, Phase 5 chose "skip" or "new post" almost every time and
produced zero threaded replies; waiting for a specific pair to spontaneously reach a
`:memo:`→✅ handshake makes the ThreadDecision assertion untestable rather than merely
slow. The seeded history is written through the real `_post_message` (so it exists in both
stores, like everything else) and is then rebuilt by the real resume path — which is the
normal case, not an edge case: `_rebuild_agent_state` runs on every restart.
"""

import asyncio
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import src.agent.simulation as sim
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import ThreadNotFound, markdown_to_mrkdwn
from src.config import get_settings as real_settings
from src.models import (
    COHORT_ACTION_TOPOLOGY_SNAPSHOT,
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    LlmCallLog,
    SimulationRun,
    ThreadDecision,
)
from src.visibility import VISIBILITY_PUBLIC

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_slack,
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="no ANTHROPIC_API_KEY — a full run costs real money and is opt-in",
    ),
]

AGENTS = ("su", "cravatt", "wiseman")

# Complementary by construction (discipline 1). Every pair needs something only the
# other two have, so no pair can fail to converse for reasons of relevance.
LABS = {
    "su": (
        "SuProbeBot",
        "genome-scale CRISPR screens mapping E3 ligases to their substrates; we need "
        "degrader chemistry to act on the hits and quantitative imaging to watch "
        "substrate loss",
    ),
    "cravatt": (
        "CravattProbeBot",
        "covalent chemoproteomics finding ligandable cysteines on E3 ligases and "
        "elaborating them into degraders; we need screen hits worth targeting and an "
        "imaging readout for degradation kinetics",
    ),
    "wiseman": (
        "WisemanProbeBot",
        "quantitative single-cell imaging of substrate degradation kinetics and the "
        "proteostasis stress response; we need screen hits to watch and degrader "
        "chemistry to perturb them with",
    ),
}

# Slack's chat.postMessage is ~1 msg/s per channel. Only the harness's own seeding posts
# fast enough to need this; a real turn takes seconds of LLM time between posts.
POST_GAP = 1.1

# Bounds. The turn count is the primary one, enforced by wrapping `_run_turn` (a signal
# the loop itself does not expose). `budget_cap` is a real ceiling the engine enforces, but
# it is deliberately set clear of the turn bound rather than used to stop the run, because
# a run stopped by budget exhaustion WEDGES instead of exiting:
#
#   `_turn_eligible` already excludes over-budget agents, so `_select_agent` returns None
#   (and the loop breaks) only once EVERY agent is over budget. With exactly one agent
#   still under budget, and that agent being `_last_llm_caller`, the loop takes the
#   `continue` branch at simulation.py:504-518 forever: no turn is taken, `turn_count`
#   never advances, `_last_llm_caller` is never cleared on that path, and nothing is
#   logged above DEBUG. Measured: su=7/7, cravatt=7/7, wiseman=4/7 and the process spun
#   until the wall-clock deadline. Reported, not fixed.
TURNS = int(os.environ.get("FULL_RUN_TURNS", "20"))
BUDGET = int(os.environ.get("FULL_RUN_BUDGET", "40"))
RESTART_TURNS_A = int(os.environ.get("FULL_RUN_RESTART_TURNS_A", "4"))
RESTART_TURNS_B = int(os.environ.get("FULL_RUN_RESTART_TURNS_B", "4"))
# api_call_count is rebuilt from llm_call_logs on resume (_rebuild_agent_state step 4), so
# the second engine's budget must be *cumulative* or it starts already exhausted.
RESTART_BUDGET_A = int(os.environ.get("FULL_RUN_RESTART_BUDGET_A", "40"))
RESTART_BUDGET_B = int(os.environ.get("FULL_RUN_RESTART_BUDGET_B", "80"))
# Wall-clock ceiling per engine. Enforced with request_stop(), never with a cancelling
# asyncio timeout: cancellation mid-await is the SIGKILL failure mode this file is about.
# Also the backstop for the livelock above, which is why it is not generous.
DEADLINE_S = float(os.environ.get("FULL_RUN_DEADLINE_S", "900"))

# Slack splits a chat.postMessage `text` longer than this into several messages and
# returns the LAST chunk's ts. Measured against this workspace today: a 16,441-char post
# became five messages of 4000/4000/4000/4000/441 characters, and the returned ts was the
# fifth. See test_a_message_over_slacks_4000_char_limit_stays_in_bijection.
SLACK_TEXT_CHUNK = 4000

_LINK_LABELLED = re.compile(r"<(?:mailto:)?[^|>\s]+\|([^>]*)>")
_LINK_BARE = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_EMOJI_SHORTCODE = re.compile(r":[a-z0-9_+'\-]+:")
_NON_WORD = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

_SYSTEM_SUBTYPES = {
    "message_deleted", "message_changed", "channel_join", "channel_leave",
    "channel_purpose", "channel_topic", "channel_name", "channel_archive",
    "channel_unarchive", "bot_add", "bot_remove",
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class RunCtx:
    """Everything a run needs, plus what the harness itself authored."""

    factory: object
    run_id: uuid.UUID
    channel: str
    channel_id: str
    clients: dict
    seed_ts: set = field(default_factory=set)
    off_channel_posts: list = field(default_factory=list)


@dataclass
class TurnRecord:
    """What the loop did, sampled per turn from inside _run_turn."""

    turns: int = 0
    errors: list = field(default_factory=list)
    gates: list = field(default_factory=list)
    gate_active: list = field(default_factory=list)
    preflight: list = field(default_factory=list)
    deadline_hit: bool = False

    def diagnosis(self) -> str:
        return (
            f"turns={self.turns} deadline_hit={self.deadline_hit} "
            f"errors={self.errors} gate_active={set(self.gate_active)} "
            f"preflight={set(self.preflight)}"
        )


@pytest.fixture
async def full_run(engine, slack_clients, slack_probe_channel, tmp_path, monkeypatch):
    """A live workspace collapsed to one `t-` channel, a 3-agent roster, one cohort.

    Deliberately not the rolled-back ``db_session``: the engine opens its own sessions
    and commits, and that is the path under test.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    name, cid = slack_probe_channel

    # Discipline 2: one channel, no keyword scatter. Rebound on the module because the
    # engine reads these globals directly from a dozen call sites during a real turn.
    monkeypatch.setattr(sim, "SEEDED_CHANNELS", [name])
    monkeypatch.setattr(sim, "_UNIVERSAL_CHANNELS", {name})
    monkeypatch.setattr(sim, "_CHANNEL_KEYWORDS", {})

    patched = real_settings().model_copy(update={
        "cohort_isolation_enabled": True,
        "cohort_default_policy": "isolated",
        "max_consecutive_reactive_turns": 3,
        "turn_delay_seconds": 0.0,
        "phase5_skip_probability": 0.0,
    })
    monkeypatch.setattr(sim, "get_settings", lambda: patched)

    # Working memory is process-external state on a shared volume
    # (profiles/memory/{agent}/public.md), written by `_update_agent_memory` on every
    # thread closure and read back into every later prompt. Left at the real path, run N+1
    # inherits run N's conclusions: measured, a second run whose memory already said
    # "closed: no_proposal" produced Phase 5 skips on nine consecutive turns. That biases
    # a run toward INCONCLUSIVE, so isolate it per test. Both bindings are patched —
    # simulation.py imports the constant by value (`from src.agent.agent import
    # PROFILES_DIR`), so patching only the source module would leave the profile-mtime
    # watcher pointed at the repo.
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(sim, "PROFILES_DIR", tmp_path / "profiles")

    ctx = RunCtx(factory=factory, run_id=run_id, channel=name, channel_id=cid,
                 clients=dict(slack_clients))

    # Discipline 6: never write outside the probe channel. `list_channels` is
    # deliberately NOT patched — see discipline 5 in the module docstring.
    for client in slack_clients.values():
        monkeypatch.setattr(
            client, "post_message", _channel_guard(client, name, cid, ctx.off_channel_posts)
        )

    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        for aid in AGENTS:
            db.add(AgentRegistry(agent_id=aid, bot_name=LABS[aid][0],
                                 pi_name=f"PI {aid}", status="active"))
        cohort = Cohort(name="t13-one-cohort")
        db.add(cohort)
        await db.flush()
        for aid in AGENTS:
            db.add(CohortMembership(cohort_id=cohort.id, agent_id=aid))
        await db.commit()

    try:
        yield ctx
    finally:
        async with factory() as db:
            await db.execute(delete(CohortAuditEvent))
            await db.execute(delete(CohortMembership))
            await db.execute(delete(Cohort))
            await db.execute(
                delete(ThreadDecision).where(ThreadDecision.simulation_run_id == run_id)
            )
            await db.execute(
                delete(LlmCallLog).where(LlmCallLog.simulation_run_id == run_id)
            )
            await db.execute(
                delete(AgentMessage).where(AgentMessage.simulation_run_id == run_id)
            )
            await db.execute(
                delete(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
            )
            # profile_revisions rows written by the memory update cascade from here.
            await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.in_(AGENTS)))
            await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
            await db.commit()


def _canonical_text(text: str) -> str:
    """Reduce a message to the content both stores can be held to.

    Slack does not store what you posted. Measured against this workspace today, three
    rewrites happen inside `chat.postMessage` before the text is ever readable back:

        'See https://doi.org/10.1038/x'  ->  'See <https://doi.org/10.1038/x>'
        '✅ and ⏸️'                       ->  ':white_check_mark: and :double_vertical_bar:'
        'Contact a@b.edu'                ->  'Contact <mailto:a@b.edu|a@b.edu>'

    Asserting byte equality against that pins Slack's own text normalisation, not our
    mirror (Rule L2), and it fails the moment an agent cites a paper or types a check
    mark — which is exactly what the ✅ close protocol asks it to do. So: unwrap Slack's
    link markup, drop emoji in *both* spellings (shortcode and the raw codepoint), drop
    punctuation, and compare the remaining words. A mirror that posted different content,
    truncated it, or swapped two messages still fails; Slack's rendering no longer does.
    """
    text = _LINK_LABELLED.sub(r"\1", text)
    text = _LINK_BARE.sub(r"\1", text)
    text = _EMOJI_SHORTCODE.sub(" ", text)
    text = text.encode("ascii", "ignore").decode("ascii")  # raw emoji, smart quotes
    text = _NON_WORD.sub(" ", text)
    return _WS.sub(" ", text).strip().lower()


def _is_fragment_of(chunk: str, whole: str) -> bool:
    """Is `chunk` a piece of `whole`? Used to tell a split fragment from a lost message.

    Compared on canonical text, and with the first and last token dropped: Slack cuts at
    a fixed character count, so both ends of a chunk are usually half a word.
    """
    words = _canonical_text(chunk).split()
    if len(words) < 5:
        return False
    return " ".join(words[1:-1]) in _canonical_text(whole)


def _channel_guard(client, allowed_name, allowed_id, sink):
    """Refuse (loudly) to post anywhere but the probe channel."""
    real = client.post_message

    def _post(channel, text, thread_ts=None):
        if channel not in (allowed_name, allowed_id):
            sink.append((client.agent_id, channel, (text or "")[:120]))
            raise RuntimeError(
                f"[{client.agent_id}] refusing to post outside the test channel: "
                f"{channel!r} (would have written into the real workspace)"
            )
        return real(channel, text, thread_ts=thread_ts)

    return _post


def _make_agents():
    agents = []
    for aid in AGENTS:
        bot, summary = LABS[aid]
        a = Agent(agent_id=aid, bot_name=bot, pi_name=f"PI {aid}")
        # The cached-profile seam: a real profile without touching disk or the DB.
        a._public_profile = f"# {aid.capitalize()} Lab\n\n{summary}\n"
        a._private_profile = "No private instructions yet."
        agents.append(a)
    return agents


def _make_engine(ctx, *, budget, bare=False):
    """A real engine with real Slack clients and slack_enabled=True.

    ``bare`` skips nothing in the engine — it only means the caller will drive
    ``_post_message`` directly instead of ``start()``, so the channel maps that
    ``_ensure_seeded_channels`` would populate are set up here instead.
    """
    eng = SimulationEngine(
        agents=_make_agents(),
        slack_clients=ctx.clients,
        max_runtime_minutes=0,
        budget_cap=budget,
        session_factory=ctx.factory,
        # `--reset-cursors` (a real production flag), and it is load-bearing here.
        # `_rebuild_agent_state` step 5 advances every agent's last_seen_cursor to
        # max(posted_at), so on a resumed run the harness's own seeded intros are
        # already "seen": Phase 2 returns nothing, interesting_posts stays empty and
        # Phase 5 has nothing to reply to. Measured without it — turn 1 concluded the
        # seeded thread and turns 2-5 were all "Agent chose to skip", then the loop
        # went idle. Resetting the cursors is what lets three agents actually discover
        # each other, which is the precondition for a multi-turn run to exist at all.
        reset_cursors=True,
        simulation_run_id=ctx.run_id,
        slack_enabled=True,
    )
    if bare:
        eng.message_log.set_persist_callback(eng._enqueue_persist)
        eng._channel_id_map = {ctx.channel: ctx.channel_id}
        eng._channel_visibility = {ctx.channel: VISIBILITY_PUBLIC}
        for a in eng.agents.values():
            a.state.subscribed_channels = {ctx.channel}
    return eng


def _bound_turns(eng, rec, limit):
    """Bound the loop by turn count and record per-turn gate state.

    The engine exposes no turn counter, and `budget_cap` alone is a blunt bound (a turn
    costs 1-3 calls). Wrapping the bound method also gives us the per-turn errors that
    `start()` otherwise only writes to the log.
    """
    real = eng._run_turn

    async def _wrapped(agent):
        rec.turns += 1
        rec.gates.append({
            a: (None if x.allowed_sender_ids is None else set(x.allowed_sender_ids))
            for a, x in eng.agents.items()
        })
        rec.gate_active.append(eng._cohort_gate_active)
        rec.preflight.append(eng._cohort_preflight_error)
        try:
            return await real(agent)
        except Exception as exc:
            rec.errors.append(f"{agent.agent_id}: {type(exc).__name__}: {exc}")
            raise
        finally:
            if rec.turns >= limit:
                eng.request_stop()

    eng._run_turn = _wrapped


async def _deadline(eng, rec, seconds):
    """Graceful wall-clock ceiling. Never cancels an in-flight await."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    rec.deadline_hit = True
    eng.request_stop()


async def _drive(eng, rec, *, turns, deadline=DEADLINE_S):
    """Run the real main loop, bounded, and flush on the way out as main.py does."""
    _bound_turns(eng, rec, turns)
    watchdog = asyncio.create_task(_deadline(eng, rec, deadline))
    try:
        await eng.start()
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


# ---------------------------------------------------------------------------
# Seeding — preconditions, excluded from every measurement
# ---------------------------------------------------------------------------


def _last_ts(eng) -> str:
    return eng.message_log._entries[-1].ts


async def _seed(ctx, *, replies: int) -> str:
    """Write the intros and one near-concluded thread through the real mirror.

    Returns the thread's root ts. Both stores see all of it: the seeds are posted with
    `_post_message`, exactly like an agent's own message, so they cannot themselves
    create the one-store-only condition the tests are looking for.

    `replies` alternates cravatt/su and the LAST reply is su's `:memo: Summary`, which
    leaves cravatt owing a reply to a thread that has an explicit proposal to confirm.
    The models still choose what to do — ✅ (proposal), their own revised `:memo:`, or
    ⏸️ (no_proposal) — and `max_thread_messages` closes it as `timeout` if they choose
    none of them. What is seeded is the *opportunity*, not the outcome.

    Strictly two-party: the root tags CravattProbeBot, so
    `MessageLog.get_thread_allowed_agents` pins the thread to {su, cravatt} and Phase 4
    aborts for anyone else. Seeding a third voice into it would build a thread the engine
    then refuses to continue. wiseman is the third agent for a reason — it has to reach
    the others through Phase 2/5 like a real participant.
    """
    seeder = _make_engine(ctx, budget=0, bare=True)

    for aid in AGENTS:
        await seeder._post_message(
            aid, ctx.channel,
            f":wave: Introducing our lab: {LABS[aid][1]}. Keen to hear from "
            "complementary groups.",
        )
        time.sleep(POST_GAP)
    await seeder._flush_persisted()

    await seeder._post_message(
        "su", ctx.channel,
        ":bulb: Concretely: our CRISPR screen has 40 E3-ligase/substrate pairs with no "
        "chemical entry point. @CravattProbeBot — what would you need from us to turn "
        "one of those pairs into a degrader with a measured kinetic readout?",
    )
    time.sleep(POST_GAP)
    await seeder._flush_persisted()
    root_ts = _last_ts(seeder)

    bodies = [
        ("cravatt", "Interested. We have covalent fragment hits on two of those "
                    "ligases already. What is the substrate turnover in your hands?"),
        ("su", "Substrate half-life is 90 minutes in the unperturbed line; the screen "
               "read out at 72 hours, so we cannot resolve kinetics ourselves."),
        ("cravatt", "Then the kinetic readout is the bottleneck, not the chemistry. We "
                    "can supply two elaborated covalent handles at 1 and 10 micromolar "
                    "plus the inactive alkyne control."),
        ("su", "We have a degron-tagged reporter line for that substrate, so a "
               "live readout is possible on our side with the right imaging."),
        ("cravatt", "Then the open question is whether engagement produces degradation "
                    "on the timescale of turnover, or only a stress response."),
        ("su", "Right — the pooled screen cannot separate those two, which is exactly "
               "why the 72-hour readout was uninformative."),
        ("cravatt", "Four arms would settle it: two doses, the alkyne control, and "
                    "vehicle, all on your reporter line."),
        ("su", ":memo: Summary — Proposal: CRISPR-nominated E3/substrate pair to "
               "measured degradation kinetics.\n"
               "What each lab brings: Su lab — validated E3/substrate pair and the "
               "degron-tagged reporter line. Cravatt lab — two elaborated covalent "
               "handles plus an inactive alkyne control.\n"
               "Scientific question: does covalent engagement of the nominated E3 "
               "produce substrate degradation on the timescale of substrate turnover, "
               "or only a proteostasis stress response?\n"
               "First experiment: four arms (2 compound doses, alkyne control, vehicle) "
               "on the reporter line, read out live over 8 hours. Two weeks from "
               "compound delivery.\n"
               "Why together: neither the pooled screen nor bulk chemoproteomics can "
               "resolve degradation on the timescale of turnover.\n"
               "Confidence: [Moderate]"),
    ]
    for aid, body in bodies[:replies]:
        await seeder._post_message(aid, ctx.channel, body, thread_ts=root_ts)
        time.sleep(POST_GAP)
    await seeder._flush_persisted()

    ctx.seed_ts = {e.ts for e in seeder.message_log._entries}
    return root_ts


# ---------------------------------------------------------------------------
# Measurement — the two stores, read independently
# ---------------------------------------------------------------------------


async def _db_snapshot(ctx) -> dict[str, AgentMessage]:
    async with ctx.factory() as db:
        rows = (await db.execute(
            select(AgentMessage)
            .where(AgentMessage.simulation_run_id == ctx.run_id)
            .order_by(AgentMessage.posted_at)
        )).scalars().all()
    return {r.message_ts: r for r in rows}


def _slack_snapshot(ctx) -> dict[str, tuple[str, str | None]]:
    """``{ts: (text, thread_ts|None)}`` for every real message in the probe channel.

    Read through a client that did not author most of them, and enumerated
    independently of the engine's own reconcile (history + one conversations.replies per
    threaded root), so the two sides of the comparison are not the same code path.
    """
    client = ctx.clients["su"]
    out: dict[str, tuple[str, str | None]] = {}
    for msg in client.get_full_channel_history(ctx.channel_id):
        ts = msg.get("ts")
        if not ts or msg.get("subtype") in _SYSTEM_SUBTYPES:
            continue
        out[ts] = (msg.get("text") or "", None)
        if not msg.get("reply_count"):
            continue
        try:
            replies = client.get_all_thread_replies(ctx.channel_id, ts)
        except ThreadNotFound:
            continue
        for r in replies:
            rts = r.get("ts")
            if not rts or rts == ts or r.get("subtype") in _SYSTEM_SUBTYPES:
                continue
            out[rts] = (r.get("text") or "", ts)
    return out


def _both_stores(ctx, db_rows):
    """(db_only, slack_only) — retried once, because Slack search-side propagation can
    lag a post by a second and a false headline is worse than a slow test."""
    slack = _slack_snapshot(ctx)
    db_only = set(db_rows) - set(slack)
    slack_only = set(slack) - set(db_rows)
    if db_only or slack_only:
        time.sleep(4.0)
        slack = _slack_snapshot(ctx)
        db_only = set(db_rows) - set(slack)
        slack_only = set(slack) - set(db_rows)
    return slack, db_only, slack_only


def _split_fragments(slack, db_rows, slack_only) -> dict[str, str]:
    """``{fragment_ts: parent_row_ts}`` for Slack-only messages the >4000-char split
    explains. Anything left over is a genuine one-store-only message."""
    out: dict[str, str] = {}
    oversized = [
        (r.message_ts, markdown_to_mrkdwn(r.content)) for r in db_rows.values()
        if len(markdown_to_mrkdwn(r.content)) > SLACK_TEXT_CHUNK
    ]
    for ts in slack_only:
        for parent_ts, whole in oversized:
            if _is_fragment_of(slack[ts][0], whole):
                out[ts] = parent_ts
                break
    return out


async def _llm_calls(ctx) -> int:
    async with ctx.factory() as db:
        return (await db.execute(
            select(func.count(LlmCallLog.id))
            .where(LlmCallLog.simulation_run_id == ctx.run_id)
        )).scalar_one()


async def _decisions(ctx):
    async with ctx.factory() as db:
        return (await db.execute(
            select(ThreadDecision).where(ThreadDecision.simulation_run_id == ctx.run_id)
        )).scalars().all()


def _agent_authored(ctx, db_rows) -> list[AgentMessage]:
    """Rows written by an agent during a turn — the harness's seeds excluded."""
    return [
        r for ts, r in db_rows.items()
        if ts not in ctx.seed_ts and r.is_bot and r.agent_id
    ]


# ===========================================================================
# T13.1 — the whole system, one pass
# ===========================================================================


async def test_a_full_run_keeps_both_stores_in_bijection(full_run):
    """Real turns, real mirror, real gate — and no message in one store only.

    The headline assertion is set equality between `agent_messages.message_ts` for this
    run and every ts Slack holds in the channel. In pure Slack-on mode the canonical id
    *is* the Slack ts, so a row with no Slack twin means the mirror silently no-oped, and
    a Slack message with no row means a message the DB-primary design has already lost.

    Everything else here is either a precondition for that assertion to mean anything
    (a conversation actually happened) or a property the same run can pay for once:
    the gate held, a thread concluded, a ThreadDecision was written, provenance recorded.
    """
    ctx = full_run
    root_ts = await _seed(ctx, replies=8)
    seed_rows = await _db_snapshot(ctx)
    assert len(seed_rows) == len(ctx.seed_ts) == 12, (
        f"seeding did not land: {len(seed_rows)} rows for {len(ctx.seed_ts)} seeds"
    )

    eng = _make_engine(ctx, budget=BUDGET)
    rec = TurnRecord()
    await _drive(eng, rec, turns=TURNS)
    await eng.stop()

    db_rows = await _db_snapshot(ctx)
    slack, db_only, slack_only = _both_stores(ctx, db_rows)
    authored = _agent_authored(ctx, db_rows)
    calls = await _llm_calls(ctx)
    decisions = await _decisions(ctx)
    where = (
        f"{rec.diagnosis()} llm_calls={calls} db={len(db_rows)} slack={len(slack)} "
        f"agent_authored={len(authored)} decisions={[(d.outcome, d.thread_id) for d in decisions]}"
    )

    # --- the control: did anything actually happen? -------------------------
    assert not rec.errors, f"a turn raised: {rec.errors}. {where}"
    assert rec.turns > 0, f"INCONCLUSIVE: the loop took no turns at all. {where}"
    assert authored, (
        "INCONCLUSIVE, NOT PASSING: no agent wrote anything in "
        f"{rec.turns} turns, so every store-consistency claim below is trivially "
        f"true and this run proves nothing. {where}"
    )
    replies_in_seeded_thread = [
        r for r in authored if r.thread_ts == root_ts
    ]
    assert replies_in_seeded_thread, (
        "INCONCLUSIVE, NOT PASSING: no real model wrote into the open thread, so no "
        f"conversation formed and nothing about threading was exercised. {where}"
    )

    # --- the headline -------------------------------------------------------
    assert not db_only, (
        "MESSAGES IN POSTGRES WITH NO SLACK TWIN — the mirror no-oped for "
        f"{len(db_only)} message(s): "
        f"{[(t, db_rows[t].agent_id, db_rows[t].content[:60]) for t in sorted(db_only)]}. {where}"
    )
    # This allowance is now expected to be EMPTY, and the cross-check below asserts it.
    # It used to absorb the one benign explanation for a Slack-only message: a post over
    # SLACK_TEXT_CHUNK arrived as several Slack messages and only the last one's ts was
    # recorded, so the earlier chunks were in Slack with no row. The client cuts at the
    # boundary itself now and the engine records one row per message, so no row can be
    # over the limit and `_split_fragments` therefore finds nothing to excuse. Kept
    # rather than deleted because it is self-neutralising — `oversized` empty forces
    # `fragments` empty — and it names, at the point of use, what a regression looks
    # like. Anything that is NOT a fragment of a message we do have is a genuine loss.
    fragments = _split_fragments(slack, db_rows, slack_only)
    unexplained = slack_only - set(fragments)
    assert not unexplained, (
        "MESSAGES IN SLACK WITH NO POSTGRES ROW — the primary store lost "
        f"{len(unexplained)} message(s), and none of them is a >{SLACK_TEXT_CHUNK}-char "
        f"split fragment of a message it does have: "
        f"{[(t, slack[t][0][:80]) for t in sorted(unexplained)]}. {where}"
    )
    assert set(db_rows) == set(slack) - set(fragments), (
        f"stores disagree beyond the characterised split. fragments={fragments}. {where}"
    )

    # --- and the mapping is usable, not merely present ----------------------
    oversized = []
    for ts, row in db_rows.items():
        assert row.slack_ts == ts, (
            f"row {ts} records slack_ts={row.slack_ts!r}; in Slack-on mode the "
            f"canonical id IS the Slack ts. {where}"
        )
        assert row.slack_channel_id == ctx.channel_id, (
            f"row {ts} points at channel {row.slack_channel_id!r}, not {ctx.channel_id}"
        )
        assert row.channel_name == ctx.channel, (
            f"row {ts} claims channel #{row.channel_name}, outside the test channel"
        )
        assert row.content.strip() and slack[ts][0].strip(), (
            f"row {ts}: an empty message reached one of the stores "
            f"(db={row.content[:40]!r} slack={slack[ts][0][:40]!r})"
        )
        full = markdown_to_mrkdwn(row.content)
        if len(full) > SLACK_TEXT_CHUNK:
            # Split by Slack: the row holds the whole message, this ts holds its tail.
            oversized.append((ts, len(full)))
            assert _is_fragment_of(slack[ts][0], full), (
                f"row {ts} is over the {SLACK_TEXT_CHUNK}-char limit and its Slack twin "
                f"is not even a piece of it.\n  db:    {row.content[:200]!r}\n"
                f"  slack: {slack[ts][0][:200]!r}"
            )
        else:
            assert _canonical_text(full) == _canonical_text(slack[ts][0]), (
                f"row {ts} and its Slack twin do not carry the same content.\n"
                f"  db:    {row.content[:200]!r}\n  slack: {slack[ts][0][:200]!r}"
            )
        # Threading agrees on both sides, translated through the mirror mapping.
        assert slack[ts][1] == row.slack_thread_ts, (
            f"row {ts}: db slack_thread_ts={row.slack_thread_ts!r}, "
            f"Slack says thread_ts={slack[ts][1]!r}"
        )
        if row.thread_ts:
            assert row.slack_thread_ts == row.thread_ts, (
                f"reply {ts} has a canonical parent {row.thread_ts} that differs from "
                f"its Slack parent {row.slack_thread_ts} — no message here was minted "
                "with Slack off, so they must agree"
            )

    assert ctx.off_channel_posts == [], (
        "an agent tried to post outside the test channel; _phase5_new_post defaults "
        f"`channel` to 'general' when the model omits it: {ctx.off_channel_posts}"
    )
    # Cross-check the allowance against its cause: a fragment may only exist because a
    # message was over the limit, and an over-limit message must produce fragments.
    assert bool(fragments) == bool(oversized), (
        f"the split allowance and its cause disagree: fragments={fragments} "
        f"oversized={oversized}. {where}"
    )

    # --- the gate held throughout -------------------------------------------
    expected_gate = set(AGENTS)
    assert rec.gates, f"no gate sample was taken. {where}"
    for i, sample in enumerate(rec.gates):
        assert sample == {a: expected_gate for a in AGENTS}, (
            f"the cohort gate changed at turn {i}: {sample}. {where}"
        )
    assert set(rec.gate_active) == {True}, (
        f"the gate reported itself inactive during the run: {rec.gate_active}. {where}"
    )
    assert set(rec.preflight) == {None}, (
        f"the cohort preflight forced isolation off mid-run: {rec.preflight}. {where}"
    )
    assert eng._cohort_tags_stripped == {}, (
        "an in-cohort @mention was stripped — every agent here shares a cohort with "
        f"every other, so the strip count must be zero: {eng._cohort_tags_stripped}"
    )

    async with ctx.factory() as db:
        snaps = (await db.execute(
            select(CohortAuditEvent).where(
                CohortAuditEvent.action == COHORT_ACTION_TOPOLOGY_SNAPSHOT,
                CohortAuditEvent.simulation_run_id == ctx.run_id,
            )
        )).scalars().all()
    assert snaps, f"the run recorded no topology snapshot, so it is unattributable. {where}"
    assert snaps[0].topology["cohort_isolation_enabled"] is True
    assert snaps[0].topology["agents"]["su"] == sorted(AGENTS)

    # --- a thread concluded, and the decision is durable --------------------
    assert decisions, (
        "no ThreadDecision was written: the thread neither reached a :memo:/✅ "
        "handshake, nor a ⏸️ close, nor the max_thread_messages backstop. "
        f"{where}"
    )
    decided = [d for d in decisions if d.thread_id == root_ts]
    assert decided, (
        f"a thread concluded but not the one under test: "
        f"{[(d.thread_id, d.outcome) for d in decisions]}. {where}"
    )
    d = decided[0]
    assert d.outcome in ("proposal", "no_proposal", "timeout"), d.outcome
    assert {d.agent_a, d.agent_b} <= set(AGENTS)
    assert d.channel == ctx.channel
    if d.outcome == "proposal":
        assert d.summary_text and ":memo:" in d.summary_text, (
            f"a proposal was recorded with no summary: {d.summary_text!r}"
        )
    # The conclusion must not have cost the mirror its consistency: re-check the
    # closed thread's Slack twin explicitly, since _close_thread runs after the post.
    assert eng._closed_thread_ids >= {root_ts}


# ===========================================================================
# T13.1b — the one-store-only condition, isolated and deterministic
#
# Found by the run above, then reduced to these two tests: no LLM calls, four Slack
# calls, and a definite answer. The >4000-char case was an xfail(strict=True) here while
# the split defect was open, which is why the run test above carries a split-fragment
# allowance; both are now closed and the pair is the cheapest end-to-end evidence for it.
# ===========================================================================


async def test_a_short_message_round_trips_one_to_one(full_run):
    """Control for the test below: the mirror IS in bijection for ordinary messages.

    Without this leg, a failure below is equally explained by the mirror being broken for
    everything, or by the probe channel being unreadable (Rule S2).
    """
    ctx = full_run
    eng = _make_engine(ctx, budget=0, bare=True)
    await eng._post_message("su", ctx.channel, "one ordinary message, well under the limit")
    time.sleep(POST_GAP)
    await eng._flush_persisted()

    db_rows = await _db_snapshot(ctx)
    slack, db_only, slack_only = _both_stores(ctx, db_rows)
    assert len(db_rows) == 1, [r.content for r in db_rows.values()]
    assert len(slack) == 1, slack
    assert not db_only and not slack_only, (db_only, slack_only)
    row = next(iter(db_rows.values()))
    assert row.slack_ts == row.message_ts
    assert _canonical_text(slack[row.message_ts][0]) == _canonical_text(row.content)


async def test_a_message_over_slacks_4000_char_limit_stays_in_bijection(full_run):
    """One `_post_message` produces one row per Slack message it really made.

    Was `xfail(strict=True)`. Slack splits a `chat.postMessage` `text` over 4000
    characters into several messages itself and returns only the LAST chunk's ts;
    `post_message` passed that single ts back, `_post_message` recorded it as the
    canonical id, and every earlier chunk existed in Slack with no `agent_messages` row.
    The consequences went past a missing row — `slack_ts` named the *tail*, so
    `_slack_parent_ts` threaded replies onto a fragment, `posted_at = float(ts)` took the
    tail's clock, and the next restart's `_rebuild_state_from_slack` saw the unrecorded
    head chunks as brand-new inbound messages and ingested them.

    Phase 4 replies are generated with `max_tokens=1500`, roughly 6000 characters, so
    this is reached by ordinary agent traffic: it is what the 20-turn run tripped over.
    The client now cuts at the boundary itself and reports every message it created, and
    the engine writes one row each — so the set equality below is exact, with no
    "characterised split fragment" allowance on either side.
    """
    ctx = full_run
    eng = _make_engine(ctx, budget=0, bare=True)
    body = "Opening sentence of a long reply. " + " ".join(
        f"Point {i} on covalent degrader kinetics and single-cell imaging." for i in range(1, 121)
    )
    assert len(body) > SLACK_TEXT_CHUNK, len(body)

    await eng._post_message("su", ctx.channel, body)
    time.sleep(POST_GAP)
    await eng._flush_persisted()

    db_rows = await _db_snapshot(ctx)
    slack, db_only, slack_only = _both_stores(ctx, db_rows)
    row = next(iter(db_rows.values()))
    detail = (
        f"posted {len(body)} chars; Slack holds {len(slack)} message(s) of lengths "
        f"{sorted(len(t) for t, _ in slack.values())}; the DB holds {len(db_rows)} row(s) "
        f"of lengths {sorted(len(r.content) for r in db_rows.values())}; the first row "
        f"recorded slack_ts={row.slack_ts} which is the "
        f"{'LAST' if row.slack_ts == max(slack) else 'first' if row.slack_ts == min(slack) else 'nth'}"
        f" of them; {len(slack_only)} chunk(s) have no row"
    )
    assert not db_only, detail
    assert set(db_rows) == set(slack), detail

    # The split really happened — otherwise the bijection above is the trivial one and
    # this test would pass just as well against a client that refused to post at all.
    assert len(slack) > 1, detail
    # Every row is a message Slack accepted whole: nothing over the limit survives, so
    # nothing was silently re-split on Slack's side behind our back.
    assert all(len(markdown_to_mrkdwn(r.content)) <= SLACK_TEXT_CHUNK
               for r in db_rows.values()), detail
    # One logical post stays ONE top-level post. Without this, the continuations arrive
    # as N fresh roots and every other agent's Phase 2 scan sees N posts for one.
    roots = [r for r in db_rows.values() if r.thread_ts is None]
    assert len(roots) == 1, (
        f"the split produced {len(roots)} top-level posts: "
        f"{[(r.message_ts, r.content[:40]) for r in roots]}"
    )
    assert row.slack_ts == min(slack), (
        "the recorded canonical id is not the FIRST Slack message — a reply threaded on "
        f"it would hang off a fragment. {detail}"
    )
    # And the whole post survived the split: no chunk lost, none duplicated.
    rejoined = re.sub(r"\s+", "", "".join(
        r.content for r in sorted(db_rows.values(), key=lambda r: float(r.message_ts))
    ))
    assert rejoined == re.sub(r"\s+", "", body), (
        "the rows do not reassemble into the posted message"
    )


# ===========================================================================
# T13.2 — SIGTERM, restart, and the property the DB-primary design exists for
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "LIVE DEFECT, pre-dating Fix 4 and recorded in 8515f65: the open-thread "
        "restore in Simulation._rebuild_agent_state does not reconstruct every "
        "open partnership across a SIGTERM. The DB-side invariant is pinned "
        "offline in tests/integration/test_state_rebuild.py, which passes — so "
        "the gap is in the live path (Slack ordering or the shutdown flush), not "
        "in the rebuild query. Unfixed, not unknown."
    ),
)
async def test_sigterm_and_restart_lose_nothing_and_duplicate_nothing(full_run):
    """Stop the engine with a real SIGTERM mid-turn, resume the same run, compare stores.

    Three separable claims, and the middle one is the one that has never been tested with
    Slack on:

    1. **The signal path works.** `main.py` installs `loop.add_signal_handler(SIGTERM,
       request_stop)`; the same wiring is installed here and a real `SIGTERM` is delivered
       to this process while a turn is in flight. The loop finishes the turn, flushes, and
       returns — it is not cancelled.
    2. **`stop()`'s flush is load-bearing.** A message is posted after the loop has
       exited: it reaches Slack synchronously but lives only in `_pending_persist`. The
       negative control asserts it is NOT yet in Postgres — that is exactly what
       `docker rm -f` (SIGKILL) destroys — and then that `stop()` recovers it.
    3. **Resume neither loses nor duplicates.** A second engine takes the same
       `simulation_run_id`, rebuilds from the DB, reconciles against Slack, and runs more
       turns. Every ts from before the restart must still be there, with unchanged
       content, exactly once; no Slack message may be ingested twice under a second
       canonical id; and the two stores must still be in bijection at the end.
    """
    ctx = full_run
    root_ts = await _seed(ctx, replies=3)

    # ---------------- phase A: run, then SIGTERM mid-turn ------------------
    eng1 = _make_engine(ctx, budget=RESTART_BUDGET_A)
    rec1 = TurnRecord()
    _bound_turns(eng1, rec1, 10 ** 6)  # bounded by the signal, not by a counter

    loop = asyncio.get_running_loop()
    fired: list[float] = []

    def _shutdown():                      # byte-for-byte the intent of main.py's handler
        eng1.request_stop()

    loop.add_signal_handler(signal.SIGTERM, _shutdown)

    async def _sigterm_when_running():
        while rec1.turns < RESTART_TURNS_A:
            await asyncio.sleep(0.2)
        fired.append(time.time())
        os.kill(os.getpid(), signal.SIGTERM)

    killer = asyncio.create_task(_sigterm_when_running())
    watchdog = asyncio.create_task(_deadline(eng1, rec1, DEADLINE_S))
    try:
        await eng1.start()
    finally:
        for t in (killer, watchdog):
            t.cancel()
        await asyncio.gather(killer, watchdog, return_exceptions=True)
        loop.remove_signal_handler(signal.SIGTERM)

    assert fired, (
        f"the watchdog never reached turn {RESTART_TURNS_A}, so no SIGTERM was sent. "
        f"{rec1.diagnosis()}"
    )
    assert not rec1.errors, f"a turn raised before the signal: {rec1.errors}"
    assert eng1._running is False and eng1._stop_event.is_set(), (
        "SIGTERM did not reach request_stop() — the loop exited for some other reason"
    )
    buffered_at_exit = len(eng1._pending_persist)

    # Claim 2: what SIGKILL would have destroyed.
    marker = f"post-signal, pre-flush {uuid.uuid4().hex[:8]}"
    await eng1._post_message("su", ctx.channel, marker)
    time.sleep(POST_GAP)
    assert eng1._pending_persist, "the post did not buffer, so the control is vacuous"
    pre_flush = await _db_snapshot(ctx)
    assert marker not in [r.content for r in pre_flush.values()], (
        "the message was already durable, so this control cannot show what the "
        "shutdown flush saves"
    )
    slack_now = _slack_snapshot(ctx)
    assert any(marker in t for t, _ in slack_now.values()), (
        "the message never reached Slack either, so nothing was at risk"
    )
    await eng1.stop()
    after_flush = await _db_snapshot(ctx)
    assert marker in [r.content for r in after_flush.values()], (
        "stop() did not flush the buffered message — a graceful shutdown loses the "
        "in-flight turn, which is the whole reason CLAUDE.md forbids `docker rm -f`"
    )

    db_a = await _db_snapshot(ctx)
    decided_a = {d.thread_id for d in await _decisions(ctx)}
    slack_a, db_only_a, slack_only_a = _both_stores(ctx, db_a)
    frag_a = _split_fragments(slack_a, db_a, slack_only_a)
    authored_a = _agent_authored(ctx, db_a)
    where_a = (
        f"phaseA turns={rec1.turns} buffered_at_exit={buffered_at_exit} "
        f"db={len(db_a)} slack={len(slack_a)} authored={len(authored_a)} "
        f"split_fragments={len(frag_a)}"
    )
    assert not db_only_a, f"DB-only before the restart: {sorted(db_only_a)}. {where_a}"
    assert not (slack_only_a - set(frag_a)), (
        "Slack-only before the restart, and not explained by the >4000-char split: "
        f"{sorted(slack_only_a - set(frag_a))}. {where_a}"
    )
    assert authored_a, (
        "INCONCLUSIVE, NOT PASSING: nothing was written by an agent before the "
        f"signal, so the restart has nothing at risk to preserve. {where_a}"
    )

    # ---------------- phase B: resume the same simulation_run_id -----------
    eng2 = _make_engine(ctx, budget=RESTART_BUDGET_B)
    rec2 = TurnRecord()
    rebuilt: dict[str, set] = {}

    # _backfill_foa_cache is the first setup step after all three rebuild passes
    # (DB -> Slack reconcile -> agent state), so it is where "what did resume
    # reconstruct" can be read before any new turn muddies it.
    original_backfill = eng2._backfill_foa_cache

    async def _snapshot_after_rebuild():
        rebuilt["log"] = {e.ts for e in eng2.message_log._entries}
        rebuilt["threads"] = {
            aid: set(a.state.active_threads) for aid, a in eng2.agents.items()
        }
        rebuilt["calls"] = {aid: a.api_call_count for aid, a in eng2.agents.items()}
        return await original_backfill()

    eng2._backfill_foa_cache = _snapshot_after_rebuild
    await _drive(eng2, rec2, turns=RESTART_TURNS_B)
    await eng2.stop()

    db_b = await _db_snapshot(ctx)
    slack_b, db_only_b, slack_only_b = _both_stores(ctx, db_b)
    authored_b = _agent_authored(ctx, db_b)
    where = (
        f"{where_a} | phaseB turns={rec2.turns} db={len(db_b)} slack={len(slack_b)} "
        f"authored={len(authored_b)} errors={rec2.errors} "
        f"rebuilt_log={len(rebuilt.get('log', ()))}"
    )

    # Claim 3a: resume reconstructed exactly what was stored — no loss, no phantoms.
    # The one permitted extra is a >4000-char split fragment: it is in Slack with no row,
    # so `_rebuild_state_from_slack` legitimately treats it as a message the DB is missing
    # and ingests it. That is the compounding cost of the split defect (each restart turns
    # the unrecorded head chunks into first-class messages), not a rebuild bug.
    assert rebuilt, f"resume never reached the rebuild snapshot point. {where}"
    missing = set(db_a) - rebuilt["log"]
    invented = rebuilt["log"] - set(db_a)
    assert not missing, (
        "the resumed engine's message log is missing rows it rebuilt from: "
        f"{sorted(missing)}. {where}"
    )
    assert invented <= set(frag_a), (
        "resume invented messages that are not even split fragments of a stored one: "
        f"{sorted(invented - set(frag_a))}. {where}"
    )
    # Conversational state, not just message rows, must survive. Which thread is a
    # measured outcome rather than an assumption: `_rebuild_agent_state` deliberately
    # skips threads that already have a ThreadDecision, and the seeded thread often
    # concludes during phase A — so requiring *that* thread back is asserting the
    # opposite of correct behaviour. Measured once: phase A concluded the seeded thread
    # and restored the two threads the agents had opened themselves.
    restored = {t for s in rebuilt["threads"].values() for t in s}
    assert restored, (
        "no open thread survived the restart on any agent, so nothing about conversational "
        f"state was preserved. threads={rebuilt['threads']} decided={decided_a}. {where}"
    )
    assert root_ts in restored or root_ts in decided_a, (
        f"the seeded thread {root_ts} neither came back as an open thread nor concluded — "
        f"it was silently dropped. restored={restored} decided={decided_a}. {where}"
    )
    assert not (restored & decided_a), (
        f"a concluded thread was reopened by the rebuild: {restored & decided_a}. {where}"
    )
    assert sum(rebuilt["calls"].values()) > 0, (
        "api_call_count did not survive the restart, so the resumed run's budget is "
        f"reset and a restart loop could spend without limit: {rebuilt['calls']}"
    )

    # Claim 3b: nothing lost.
    lost = set(db_a) - set(db_b)
    assert not lost, (
        f"the restart LOST {len(lost)} message(s) from Postgres: "
        f"{[(t, db_a[t].content[:60]) for t in sorted(lost)]}. {where}"
    )
    lost_slack = set(slack_a) - set(slack_b)
    assert not lost_slack, f"messages vanished from Slack: {sorted(lost_slack)}. {where}"

    # Claim 3c: nothing duplicated. The failure mode is specific — the Slack reconcile
    # re-ingesting a message it already has under a *different* canonical id — so the
    # test is on slack_ts uniqueness, not on the (constraint-enforced) message_ts.
    slack_ts_seen: dict[str, list[str]] = {}
    for ts, row in db_b.items():
        if row.slack_ts:
            slack_ts_seen.setdefault(row.slack_ts, []).append(ts)
    dupes = {s: v for s, v in slack_ts_seen.items() if len(v) > 1}
    assert not dupes, (
        f"one Slack message is represented by several rows: {dupes}. {where}"
    )
    frag_b = _split_fragments(slack_b, db_b, slack_only_b)
    assert not db_only_b, f"DB-only after the restart: {sorted(db_only_b)}. {where}"
    assert not (slack_only_b - set(frag_b)), (
        "Slack-only after the restart, and not explained by the >4000-char split: "
        f"{sorted(slack_only_b - set(frag_b))}. {where}"
    )
    assert set(db_b) == set(slack_b) - set(frag_b), (
        f"stores disagree beyond the characterised split. fragments={frag_b}. {where}"
    )

    # Claim 3d: and the surviving rows were not rewritten by the resume. The flush
    # upserts with ON CONFLICT DO UPDATE, so a rebuild that reconstructed an entry
    # slightly differently would silently clobber the original.
    for ts, before in db_a.items():
        after = db_b[ts]
        assert after.content == before.content, (
            f"row {ts} was rewritten across the restart:\n  before {before.content[:120]!r}"
            f"\n  after  {after.content[:120]!r}"
        )
        assert (after.agent_id, after.thread_ts, after.is_bot, after.channel_name) == (
            before.agent_id, before.thread_ts, before.is_bot, before.channel_name
        ), f"row {ts} metadata changed across the restart"

    assert ctx.off_channel_posts == [], ctx.off_channel_posts
    assert set(rec2.preflight) <= {None}, (
        f"the gate turned itself off after the restart: {rec2.preflight}. {where}"
    )
    for sample in rec2.gates:
        assert sample == {a: set(AGENTS) for a in AGENTS}, (
            f"the gate did not survive the restart intact: {sample}. {where}"
        )
