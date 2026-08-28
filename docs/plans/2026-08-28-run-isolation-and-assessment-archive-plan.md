# Run Isolation & Assessment Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a `--fresh` run's bots genuinely blind to every previous run's
posts, and make the assessments run dropdown a working cross-version archive —
so humans can compare how different rubric/system versions performed.

**Architecture:** Three thin, independent seams. (1) Engine: add the missing
`simulation_run_id` predicates to the four unscoped startup reads, close the
orphan-thread hole those predicates unmask, and make `--fresh` archive-and-reset
`profiles/memory/*`. (2) Web: a read-side *revision registry*
(`prompts/rubric/revisions.toml` + `src/services/rubric_revisions.py`) so each
stored assessment renders against the rubric revision that scored it, never the
live document; list pages gain per-row/per-run provenance. (3) Runs: stamp
`SimulationRun.config` with the rubric version/hash at creation. **No alembic
migration anywhere in this plan** — no DDL changes; `0039` stays free.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Jinja2 / pytest
(asyncio_mode=auto, testcontainers Postgres for `integration` marker) / TOML
(stdlib `tomllib`).

**Spec:** `docs/audits/2026-08-28-run-isolation-and-assessment-archive/README.md`
(findings F1–F5, A1–A9, recommendations R0–R7). R0 (never purge; render
version-aware) is the policy every task assumes.

## Global Constraints

- **The working tree is concurrently active.** Another workstream
  (specialist-verdict-vocabulary Phase B) has uncommitted edits in
  `src/agent/simulation.py`, `src/agent/specialists.py`, `src/agent/tools.py`,
  `src/agent/thread_guidance.py`, `prompts/roles/scout_hub/*`,
  `prompts/specialists/*`, `tests/unit/test_specialists.py`, and
  `docker-compose.prod.yml` (whose diff is deploy-critical and must NEVER be
  reverted — see CLAUDE.md's two-stack warning). Never run `git add -A`,
  `git add .`, `git stash`, `git checkout -- <file>` or `git restore`. `git add`
  only the exact files each task's commit step names.
- **Line numbers below were measured 2026-08-28 ~13:45 CDT and WILL drift.**
  Every edit is anchored by a quoted code snippet; locate by content
  (`grep -n`), treat line numbers as hints only.
- **Run pytest on the HOST, never through the sshfs mount** (CLAUDE.md: sshfs
  pytest is 100–400× slower; sshfs `pip install` corrupts `.venv-test`). The
  host checkout is `ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com:
  /home/ubuntu/blackbird-copi-science`, venv `.venv-test` (Python 3.12.3,
  verified). Test commands below are written as `ssh … pytest …`; file edits
  through the sshfs mount are fine.
- Before pushing: run the full gate on the host —
  `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && ./scripts/ci.sh'`.
  Per task, run only the named test files.
- **Never start, stop or restart the simulation or the web containers as part
  of this plan.** Deployment/restart is the operator's call (recorded
  preference). After merging, flag that both the web tier AND the agent image
  need rebuilding (`src/` is baked into both; `prompts/` is bind-mounted into
  `blackbird-app` and `agent` only), and that the branch also carries the
  already-written migration `0038` → the standard build → `alembic upgrade
  head` from a one-off container → start ordering from CLAUDE.md applies to the
  eventual deploy.
- Commit messages follow the repo's conventional style
  (`fix(agents): …`, `feat(assessments): …`, `docs: …`) and end with
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Stored data is sacred: nothing in this plan deletes or rewrites any DB row.
  Task 9 (restore) is operator-gated and additive.
- The gating vocabulary, stored `band`/`weighted_score`, and every engine write
  path are untouched. This plan changes reads, prompts-adjacent hygiene, and
  one JSON config payload.

---

### Task 1: Run-scope the four unscoped startup reads in `_rebuild_agent_state`

The engine's startup rebuild loads `thread_decisions` and `proposal_reviews`
with no `simulation_run_id` predicate, so a `--fresh` run's Phase-5 prompts
carry previous runs' interview summaries ("Do NOT re-pitch…"), agents start
benched by dead proposals, and `_closed_thread_ids` inherits every run's
thread ids. The column exists (`ThreadDecision.simulation_run_id`, NOT NULL,
`src/models/agent_activity.py:323`) and every sibling read already filters on
it; these four just don't.

**Files:**
- Modify: `src/agent/simulation.py` (four query sites + two guards; anchors below)
- Test: `tests/integration/test_state_rebuild.py` (extend)

**Interfaces:**
- Consumes: existing helpers in `tests/integration/test_state_rebuild.py`:
  `_engine_for(session, run_id, agent_ids=("su","wiseman"))`,
  `_FixtureSessionFactory`, `tests.factories.make_simulation_run`,
  `make_thread_decision`, `make_agent_message`.
- Produces: `SimulationEngine._rebuild_agent_state()` that reads only the
  current run's `thread_decisions`/`proposal_reviews`. Task 2 depends on this
  landing together with it (see the masking note there).

- [ ] **Step 1: Write the failing tests** (append to
  `tests/integration/test_state_rebuild.py`; the file already sets
  `pytestmark = pytest.mark.integration` and asyncio_mode is auto, so bare
  `async def` tests are the convention):

```python
async def test_the_rebuild_ignores_another_runs_thread_decisions(db_session):
    """A --fresh run must not inherit prior runs' interview outcomes: an
    unfiltered thread_decisions read fed every earlier run's closing summaries
    into Phase-5 prompts as 'you already pitched this' (audit F2)."""
    run = await factories.make_simulation_run(db_session)
    other = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(
        db_session, run=other, thread_id="9999.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="no_proposal",
        summary_text="FOREIGN-RUN-SUMMARY",
    )
    # Same-run control: over-filtering would be its own regression.
    await factories.make_thread_decision(
        db_session, run=run, thread_id="7777.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="no_proposal",
        summary_text="THIS-RUN-SUMMARY",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    pair = tuple(sorted(["su", "wiseman"]))
    summaries = [t["summary"] for t in eng._prior_threads.get(pair, [])]
    assert summaries == ["THIS-RUN-SUMMARY"], summaries
    assert "7777.000100" in eng._closed_thread_ids
    assert "9999.000100" not in eng._closed_thread_ids, (
        "another run's closed-thread ids leaked into this run's closed set"
    )


async def test_the_rebuild_ignores_another_runs_proposals(db_session):
    """A prior run's unreviewed proposal must not bench a fresh run's agent,
    and a prior run's collab_private proposal must not pre-finalize a
    same-named private channel (audit F3)."""
    run = await factories.make_simulation_run(db_session)
    other = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(
        db_session, run=other, thread_id="8888.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="proposal",
    )
    await factories.make_thread_decision(
        db_session, run=other, thread_id="8888.000200", channel="prv-su-wiseman",
        agent_a="su", agent_b="wiseman", outcome="proposal",
        origin_visibility="collab_private",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    assert eng.agents["su"].state.pending_proposals == []
    assert eng.agents["wiseman"].state.pending_proposals == []
    assert "prv-su-wiseman" not in eng._finalized_private_channels


async def test_the_rebuild_still_loads_this_runs_proposals(db_session):
    """Positive control for the new filter."""
    run = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(
        db_session, run=run, thread_id="6666.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="proposal",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    assert [p.thread_id for p in eng.agents["su"].state.pending_proposals] == ["6666.000100"]
```

  Note: `make_thread_decision` passes `**overrides` straight into
  `ThreadDecision(**data)`, and the model has `summary_text` and
  `origin_visibility` columns — no factory change needed. If
  `origin_visibility` turns out to be non-defaultable in the first run, add
  `origin_visibility="public"` to the two public-decision calls rather than
  touching the factory.

- [ ] **Step 2: Run the new tests to verify they fail** (leak: summaries will
  contain both, pending_proposals non-empty):

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_state_rebuild.py -q'`
Expected: the two `ignores_another_runs` tests FAIL (foreign summary present /
pending_proposals populated); every pre-existing test PASSES. (The third new
test — the positive control — passes at RED too; that is the point of it.)

- [ ] **Step 3: Apply the four filters and two guard tightenings** in
  `src/agent/simulation.py`:

  (a) In `_rebuild_agent_state`, the closed-threads/`_prior_threads` block
  (anchor: the comment `# Get all closed thread IDs and prior thread summaries
  from thread_decisions`, guard at ~:6696, query at ~:6700):

```python
# BEFORE
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(sa_select(ThreadDecision))
# AFTER
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(ThreadDecision).where(
                            ThreadDecision.simulation_run_id == self.simulation_run_id
                        )
                    )
```

  (b) The pending-proposals block (anchor: `# 3. Rebuild pending_proposals per
  agent`, guard at ~:6806, queries at ~:6810–:6822):

```python
# BEFORE
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    proposals_result = await db.execute(
                        sa_select(ThreadDecision).where(
                            ThreadDecision.outcome == "proposal"
                        )
                    )
                    proposals = proposals_result.scalars().all()

                    reviewed_result = await db.execute(
                        sa_select(
                            ProposalReview.thread_decision_id,
                            ProposalReview.agent_id,
                        )
                    )
# AFTER
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    proposals_result = await db.execute(
                        sa_select(ThreadDecision).where(
                            ThreadDecision.outcome == "proposal",
                            ThreadDecision.simulation_run_id == self.simulation_run_id,
                        )
                    )
                    proposals = proposals_result.scalars().all()

                    # ProposalReview has no simulation_run_id column
                    # (src/models/agent_registry.py) — scope it through its
                    # decision. Reviews of other runs' decisions can never
                    # match the filtered `proposals` keys anyway; the subquery
                    # keeps the read honest and small.
                    reviewed_result = await db.execute(
                        sa_select(
                            ProposalReview.thread_decision_id,
                            ProposalReview.agent_id,
                        ).where(
                            ProposalReview.thread_decision_id.in_(
                                sa_select(ThreadDecision.id).where(
                                    ThreadDecision.simulation_run_id
                                    == self.simulation_run_id
                                )
                            )
                        )
                    )
```

  (`_finalized_private_channels` needs no separate edit — it derives from the
  now-filtered `proposals` list.)

  (c) `_rebuild_state_from_db`'s NOT-IN subquery (anchor:
  `closed_thread_ids_subq = sa_select(ThreadDecision.thread_id)`, ~:5872):

```python
# BEFORE
        closed_thread_ids_subq = sa_select(ThreadDecision.thread_id)
# AFTER
        closed_thread_ids_subq = sa_select(ThreadDecision.thread_id).where(
            ThreadDecision.simulation_run_id == self.simulation_run_id
        )
```

  (d) Update the two code comments that justified the old shape if any claim
  "all runs" behaviour, and fix the stale isolation claim in
  `src/agent/main.py::_open_fresh_run`'s docstring: the sentence "every
  ``AgentMessage`` read in the startup path and the main loop is already
  run-scoped" stays true, but append: "``thread_decisions`` and
  ``proposal_reviews`` reads were the exception until 2026-08-28 and are now
  filtered too."

- [ ] **Step 4: Run the extended file, then the engine-adjacent suites**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_state_rebuild.py tests/integration/test_proposal_review.py tests/integration/test_cohort_scenarios.py -q'`
Expected: PASS. (Neither `test_proposal_review.py` nor `test_cohort_scenarios.py`
drives `_rebuild_agent_state` — `test_state_rebuild.py` is the only suite that
does; the other two run here as blast-radius insurance only. If either fails,
the filter has touched something this plan did not anticipate — stop and
reassess rather than patching tests.)

- [ ] **Step 5: Commit**

```bash
git add src/agent/simulation.py src/agent/main.py tests/integration/test_state_rebuild.py
git commit -m "fix(agents): run-scope thread_decisions/proposal_reviews startup reads

A --fresh run inherited every prior run's interview summaries into Phase-5
prompts, started agents benched on dead proposals, and polluted
_closed_thread_ids across runs. The simulation_run_id column existed and every
sibling read filtered on it; these four did not. Audit F2/F3, docs/audits/
2026-08-28-run-isolation-and-assessment-archive.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Evict a thread whose root is missing from the run-scoped log

Task 1 unmasks this: cross-run `_closed_thread_ids` pollution was the only
thing blocking Phase 3/4 from acting on a reply ingested into a previous run's
thread (possible today only via another workspace bot's `thread_broadcast`
reply — the poller mirrors bot messages including their `thread_ts`,
`simulation.py` `_poll_slack_for_bot_messages`). Without a root in the log,
`get_thread_history` returns just the reply (ordinal restarts at 2/EXPLORE)
and `get_thread_allowed_agents` returns `None` ("open to anyone"). **Ship this
task in the same PR/push as Task 1 — never Task 1 alone.**

**Files:**
- Modify: `src/agent/simulation.py` (`_reply_to_thread`, anchor at ~:1956)
- Test: `tests/integration/test_state_rebuild.py` (append; it already has the
  engine harness)

**Interfaces:**
- Consumes: `MessageLog.get_entry(ts) -> LogEntry | None`
  (`src/agent/message_log.py:345`); `LogEntry` dataclass
  (`message_log.py:28`: ts, channel, sender_agent_id, sender_name, content,
  thread_ts=None, posted_at=0.0, is_bot=True, …); `ThreadState`
  (`src/agent/state.py:9`: thread_id, channel, other_agent_id,
  message_count=0, has_pending_reply=False, …).
- Produces: `_reply_to_thread` returns early (no LLM call, no post) and evicts
  when the thread's root is absent from the log, pinning the id into
  `self._closed_thread_ids` so Phase 3 cannot re-activate it next tick.

- [ ] **Step 1: Write the failing test** (append to
  `tests/integration/test_state_rebuild.py`; add
  `from src.agent.message_log import LogEntry` and
  `from src.agent.state import ThreadState` to its imports):

```python
async def test_a_thread_with_no_root_in_the_log_is_evicted_not_replied(
    db_session, monkeypatch
):
    """A reply ingested into a thread whose parent this run never saw (e.g. a
    foreign bot's thread_broadcast into a PREVIOUS run's interview) must be
    evicted, not answered: get_thread_history would hand the LLM a one-message
    'history' and restart a concluded interview at ordinal 2 (audit F4).

    Nothing else on the unguarded path stops this turn — the participation
    check waves a missing root through (allowed is None) and budget_cap=0 is
    the INERT legacy cap, not zero budget — so without the monkeypatches the
    RED run would reach a real Anthropic call.
    """
    run = await factories.make_simulation_run(db_session)
    eng = _engine_for(db_session, run.id)
    su = eng.agents["su"]

    monkeypatch.setattr(su, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _must_not_run(**kwargs):
        raise AssertionError("an orphan thread must never reach the model")

    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _must_not_run)

    orphan_root = "1111.000100"
    eng.message_log.append(LogEntry(
        ts="1111.000200", channel="general",
        sender_agent_id="wiseman", sender_name="WisemanBot",
        content="reply into a previous run's thread",
        thread_ts=orphan_root, posted_at=1111.0002, is_bot=True,
    ))
    thread = ThreadState(
        thread_id=orphan_root, channel="general", other_agent_id="wiseman",
        message_count=1, has_pending_reply=True,
    )
    su.state.active_threads[orphan_root] = thread

    await eng._reply_to_thread(su, thread)

    assert orphan_root not in su.state.active_threads, "orphan thread not evicted"
    assert orphan_root in eng._closed_thread_ids, (
        "eviction must pin the id closed or Phase 3 re-activates it next tick"
    )
```

  (If `build_phase4_prompt`'s return shape differs from `("sys", [])`, mirror
  whatever `tests/integration/test_empty_reply_drop.py`'s monkeypatch of the
  same method returns — the sibling harnesses all patch it.)

- [ ] **Step 2: Run it to verify it fails**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_state_rebuild.py::test_a_thread_with_no_root_in_the_log_is_evicted_not_replied -q'`
Expected: FAIL with `AssertionError: an orphan thread must never reach the
model` — positive proof that the unguarded path composes a reply for a thread
whose root this run has never seen.

- [ ] **Step 3: Insert the guard** in `_reply_to_thread`, immediately after the
  history load (anchor: the exact line
  `history_entries = self.message_log.get_thread_history(thread.thread_id)`,
  ~:1956), BEFORE `thread_history = [` and the participation check:

```python
        # A thread whose ROOT is absent from this run's log is not ours to
        # continue. The only way to get here is a reply ingested into a thread
        # this run never saw — e.g. another workspace bot's thread_broadcast
        # reply landing in a PREVIOUS run's interview (conversations.history
        # returns broadcasts, and the poller mirrors every bot message,
        # thread_ts included). Replying would restart that interview at
        # ordinal 2 with a one-message "history"; get_thread_allowed_agents
        # cannot block it because a missing root reads as "open to anyone".
        # Evict, and pin the id closed so Phase 3's tag/reply scans (which all
        # check _closed_thread_ids) cannot re-activate it on the next tick.
        if self.message_log.get_entry(thread.thread_id) is None:
            logger.warning(
                "[%s] Phase 4: evicting thread %s in #%s — no root in this "
                "run's log (reply into a previous run's thread?)",
                agent.agent_id, thread.thread_id, thread.channel,
            )
            agent.state.active_threads.pop(thread.thread_id, None)
            self._closed_thread_ids.add(thread.thread_id)
            return
```

  Safety argument to verify in-place while editing (do not skip): every
  legitimate active thread in a run has its root in the log — roots enter via
  the poller (bot posts, appended with the root's own ts), the agent's own
  posts, `_rebuild_state_from_db` (run-scoped, loads roots), and
  `_hydrate_thread_from_db`; PI web-inbox messages are appended too. If while
  verifying you find a path that activates a thread without its root, STOP and
  report rather than shipping an eviction that could silence a live interview.

- [ ] **Step 3b: Seed the root in the one test harness that drives
  `_reply_to_thread` over an empty log.** `_drive_a_consult`
  (`tests/integration/test_specialist_consult_capture.py:76-171`, ~22 call
  sites) builds a `ThreadState(thread_id="t1", message_count=5, …)` and calls
  `_reply_to_thread` without ever appending "t1" to the message log — its
  intent is clearly a rooted mid-interview thread, so the guard would evict it
  and all ~22 tests would fail. Inside `_drive_a_consult`, before the drive,
  add:

```python
    sim.message_log.append(LogEntry(
        ts="t1", channel=channel, sender_agent_id="wang", sender_name="WangBot",
        content="the lab's post", thread_ts=None, posted_at=1.0,
    ))
```

  and `from src.agent.message_log import LogEntry` to that file's imports.
  One root entry is the minimal safe seed: with a single participant,
  `get_thread_allowed_agents` still returns `None` (message_log.py:598-601),
  so the helper's participation behaviour is unchanged. (Every other
  `_reply_to_thread` harness — `test_empty_reply_drop.py:71-72`,
  `test_hub_assessment_capture_gate.py:182-184`,
  `test_opportunity_assessment_persistence.py:879-881`,
  `test_truncated_engine_replies.py:174` — already seeds `ts="t1"` as the
  root and needs nothing.)

- [ ] **Step 4: Run the file plus every reply-path suite**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_state_rebuild.py tests/integration/test_specialist_consult_capture.py tests/integration/test_opportunity_assessment_persistence.py tests/integration/test_hub_assessment_capture_gate.py tests/integration/test_empty_reply_drop.py tests/integration/test_truncated_engine_replies.py -q'`
Expected: PASS. If anything else fails on a missing root, apply the Step-3b
treatment only where the fixture's intent was clearly a rooted thread;
otherwise stop and reassess the guard.

- [ ] **Step 5: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_state_rebuild.py tests/integration/test_specialist_consult_capture.py
git commit -m "fix(agents): evict threads whose root is absent from the run-scoped log

Pairs with the thread_decisions run filter: cross-run _closed_thread_ids
pollution was accidentally blocking replies into previous runs' threads.
Audit F4.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `--fresh` archives and resets `profiles/memory/*`

`profiles/memory/{agent_id}/public.md` is a cross-run verdict ledger injected
into every system prompt (`Agent._compose_working_memory`,
`src/agent/agent.py`) and survives `--fresh` by design. Per the operator's
requirement ("all previous posts fully disregarded"), `--fresh` now moves the
whole memory tree into `profiles/memory/archive/<UTC stamp>/` before the run
opens; a plain resume keeps memory untouched. No agent-side change is needed:
`Agent.public_working_memory` returns `""` when the files are gone
(agent.py:173-179 — lazy, per-instance cached, nothing preloads it at import),
`Agent._compose_working_memory` then renders `*No working memory yet — this is
your first simulation.*` (agent.py:372-373), and `update_working_memory_file`
does `mkdir(parents=True)` on write.

**Files:**
- Create: `src/agent/working_memory_reset.py`
- Modify: `src/agent/main.py` (the `if fresh:` branch in `_run_simulation`,
  the `--fresh` typer help text at ~:48-56, `_open_fresh_run`'s "Not reset"
  docstring paragraph at ~:124)
- Modify: `src/services/user_deletion.py` (`_agent_paths`, anchor
  `_MEMORY_DIR / agent_id,  # partitioned memory directory`)
- Test: `tests/unit/test_working_memory_reset.py` (new),
  `tests/integration/test_user_deletion_service.py` (extend)

**Interfaces:**
- Produces: `archive_working_memory(memory_dir: Path, *, now: float | None = None) -> Path | None`
  — moves every entry of `memory_dir` except `archive/` into
  `memory_dir/archive/<%Y%m%dT%H%M%SZ>/` (suffix `-1`, `-2`… on collision);
  returns the destination, or `None` when there is nothing to move or
  `memory_dir` does not exist.
- Consumes: `PROFILES_DIR` from `src/agent/agent.py` (`Path("profiles")`,
  agent.py:17 — relative, resolved against the process CWD exactly like every
  existing memory read/write, so archive and reader agree by construction).

- [ ] **Step 1: Write the failing unit tests** — create
  `tests/unit/test_working_memory_reset.py`:

```python
"""--fresh must reset working memory (archive, never delete): the files are a
cross-run verdict ledger injected into every system prompt (audit F1)."""
from pathlib import Path

from src.agent.working_memory_reset import archive_working_memory


def _seed_memory(memory: Path) -> None:
    (memory / "blackbird").mkdir(parents=True)
    (memory / "blackbird" / "public.md").write_text("verdict ledger\n")
    (memory / "blackbird" / "private").mkdir()
    (memory / "blackbird" / "private" / "C123.md").write_text("private notes\n")
    (memory / "agre.md").write_text("legacy flat file\n")


def test_archive_moves_agent_dirs_and_legacy_files(tmp_path):
    memory = tmp_path / "memory"
    _seed_memory(memory)

    dest = archive_working_memory(memory, now=1787900000.0)

    assert dest is not None and dest.parent == memory / "archive"
    assert not (memory / "blackbird").exists()
    assert not (memory / "agre.md").exists()
    assert (dest / "blackbird" / "public.md").read_text() == "verdict ledger\n"
    assert (dest / "blackbird" / "private" / "C123.md").exists()
    assert (dest / "agre.md").exists()


def test_archive_returns_none_when_there_is_nothing_to_move(tmp_path):
    assert archive_working_memory(tmp_path / "absent") is None
    empty = tmp_path / "memory"
    empty.mkdir()
    assert archive_working_memory(empty) is None


def test_archive_never_touches_prior_archives_and_never_collides(tmp_path):
    memory = tmp_path / "memory"
    _seed_memory(memory)
    first = archive_working_memory(memory, now=1787900000.0)
    _seed_memory(memory)
    second = archive_working_memory(memory, now=1787900000.0)

    assert first != second, "same-second fresh starts must not collide"
    assert (first / "blackbird" / "public.md").exists(), "prior archive disturbed"
    assert (second / "blackbird" / "public.md").exists()
    # Only 'archive' remains in the live tree.
    assert [p.name for p in memory.iterdir()] == ["archive"]
```

- [ ] **Step 2: Run to verify they fail** (module does not exist):

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_working_memory_reset.py -q'`
Expected: FAIL with `ModuleNotFoundError: src.agent.working_memory_reset`.

- [ ] **Step 3: Create `src/agent/working_memory_reset.py`:**

```python
"""Archive-and-reset of ``profiles/memory/*`` for ``--fresh`` runs.

Working memory is an LLM-synthesized, cross-run verdict ledger injected into
every agent system prompt (``Agent._compose_working_memory``). A ``--fresh``
run exists to be a clean experiment, so it must not start with the previous
runs' screening history in its prompts — but the files are also the only
record of what the agents "learned", so they are MOVED, never deleted.

Plain (non ``--fresh``) resumes never call this: memory continuity across a
restart of the SAME run is the point of the files.
"""
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVE_DIR_NAME = "archive"


def archive_working_memory(memory_dir: Path, *, now: float | None = None) -> Path | None:
    """Move every entry of ``memory_dir`` (except the archive itself) into
    ``memory_dir/archive/<UTC stamp>/``.

    Returns the archive directory, or ``None`` when ``memory_dir`` does not
    exist or holds nothing but prior archives. Same-filesystem ``Path.rename``
    moves — the memory tree and its archive live under one bind mount.
    """
    if not memory_dir.is_dir():
        return None
    entries = [p for p in memory_dir.iterdir() if p.name != ARCHIVE_DIR_NAME]
    if not entries:
        return None

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    dest = memory_dir / ARCHIVE_DIR_NAME / stamp
    n = 1
    while dest.exists():
        dest = memory_dir / ARCHIVE_DIR_NAME / f"{stamp}-{n}"
        n += 1
    dest.mkdir(parents=True)

    for path in entries:
        path.rename(dest / path.name)
    logger.info(
        "Archived working memory for %d agent(s) to %s", len(entries), dest
    )
    return dest
```

- [ ] **Step 4: Run the unit tests**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_working_memory_reset.py -q'`
Expected: PASS.

- [ ] **Step 5: Wire it into `--fresh`** in `src/agent/main.py`, inside
  `_run_simulation` — **above** the `if not no_db:` block, so `--fresh
  --no-db` archives too (the `if fresh:` run-minting branch sits inside
  `if not no_db:`; hooking there would leave the `--no-db` combination
  running on the old ledger). Anchor: the three lines

```python
    # Set up database session factory
    session_factory = None
    simulation_run_id = None
```

  Insert immediately BEFORE that anchor:

```python
    if fresh:
        # A fresh run must not carry previous runs' synthesized verdict
        # ledgers into its prompts (they are injected into EVERY system
        # prompt via Agent._compose_working_memory). Archive-and-reset
        # BEFORE the run opens; nothing is deleted. Plain resumes never
        # touch memory. Deliberately outside the no_db branch: --fresh
        # --no-db must start blind too. See docs/audits/
        # 2026-08-28-run-isolation-and-assessment-archive (F1).
        from src.agent.agent import PROFILES_DIR
        from src.agent.working_memory_reset import archive_working_memory

        archived_to = archive_working_memory(PROFILES_DIR / "memory")
        if archived_to is not None:
            logger.info("--fresh: working memory archived to %s", archived_to)
        else:
            logger.info("--fresh: no working memory to archive")
```

  Then update the two texts that now lie:
  - the `--fresh` typer help (anchor: `"pre-run Slack history is skipped
    rather than re-imported. Does not "` / `"reset profiles/memory/*."`) →
    `"pre-run Slack history is skipped rather than re-imported. Archives "
    "profiles/memory/* to profiles/memory/archive/<UTC stamp>/ so agents "
    "start with no working memory."`
  - `_open_fresh_run`'s docstring paragraph (anchor: `Not reset, deliberately
    and as before: ``profiles/memory/*``.`) → replace the two-sentence
    paragraph with: `Working memory is handled by the caller: ``--fresh``
    archives ``profiles/memory/*`` to ``profiles/memory/archive/<stamp>/``
    (see ``src.agent.working_memory_reset``) so a fresh run's agents start
    with none, while plain resumes keep it.`

  (CLAUDE.md's `--fresh` paragraph also still says memory "is still not
  reset"; Task 8 corrects it. If Task 8 is ever dropped, make that CLAUDE.md
  edit here instead — the doc must not contradict shipped behaviour.)

- [ ] **Step 6: Extend the deletion teardown** — archived copies of a deleted
  PI's memory must not outlive the account. In `src/services/user_deletion.py`
  change `_agent_paths` (anchor shown in Files):

```python
# BEFORE
    return [
        _PUBLIC_DIR / f"{agent_id}.md",
        _MEMORY_DIR / f"{agent_id}.md",  # legacy pre-partition memory file
        _MEMORY_DIR / agent_id,  # partitioned memory directory
    ]
# AFTER
    paths = [
        _PUBLIC_DIR / f"{agent_id}.md",
        _MEMORY_DIR / f"{agent_id}.md",  # legacy pre-partition memory file
        _MEMORY_DIR / agent_id,  # partitioned memory directory
    ]
    # --fresh archives whole memory trees under archive/<stamp>/; a deleted
    # PI's synthesized memory must be purged from those snapshots too.
    archive_root = _MEMORY_DIR / "archive"
    if archive_root.is_dir():
        paths.extend(sorted(archive_root.glob(f"*/{agent_id}")))
        paths.extend(sorted(archive_root.glob(f"*/{agent_id}.md")))
    return paths
```

  And append a test to `tests/integration/test_user_deletion_service.py`
  (mirror that file's existing style for exercising `_delete_agent_files` /
  the service — read the file first; if it drives the full service, seed the
  archive dirs with `monkeypatch.setattr` of `user_deletion._MEMORY_DIR` /
  `_PUBLIC_DIR` to `tmp_path` subdirs the way its existing file-cleanup test
  does; if no file-cleanup test exists, add a direct unit-style test):

```python
async def test_deletion_purges_archived_working_memory(tmp_path, monkeypatch):
    import uuid

    from src.services import user_deletion as ud

    memory = tmp_path / "memory"
    (memory / "archive" / "20260828T120000Z" / "agre").mkdir(parents=True)
    (memory / "archive" / "20260828T120000Z" / "agre" / "public.md").write_text("x")
    (memory / "archive" / "20260828T120000Z" / "other").mkdir()
    monkeypatch.setattr(ud, "_MEMORY_DIR", memory)
    monkeypatch.setattr(ud, "_PUBLIC_DIR", tmp_path / "public")

    report = ud.DeletionReport(user_id=uuid.uuid4(), orcid="0000-0000-0000-0000")
    ud._delete_agent_files("agre", report)

    assert not (memory / "archive" / "20260828T120000Z" / "agre").exists()
    assert (memory / "archive" / "20260828T120000Z" / "other").exists()
    assert not report.errors
```

  (`DeletionReport` is a dataclass whose `user_id`/`orcid` fields have no
  defaults — `user_deletion.py:67-71`; the module's own caller constructs it
  the same way at `:120`. `_agent_paths` reads `_MEMORY_DIR` inside the
  function body, so the module-level monkeypatch works — the file's existing
  tests use exactly this technique. Note the file's `pytestmark` is
  `pytest.mark.asyncio`, not `integration`, and this test needs no
  `db_session` — that is consistent with its siblings.)

- [ ] **Step 7: Run the affected suites**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_working_memory_reset.py tests/integration/test_user_deletion_service.py -q'`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent/working_memory_reset.py src/agent/main.py src/services/user_deletion.py tests/unit/test_working_memory_reset.py tests/integration/test_user_deletion_service.py
git commit -m "feat(agents): --fresh archives profiles/memory/* so fresh runs start blind

Working memory is a cross-run verdict ledger injected into every system
prompt; --fresh now moves it to profiles/memory/archive/<stamp>/ (deletes
nothing). Deletion teardown purges a removed PI's archived copies too.
Audit F1.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: The rubric revision registry

Stored assessments are stamped `(rubric_version, rubric_content_hash)` but the
documents that scored them are gone from the working tree, so the read paths
render every row against the live document (audit A3). This task adds a small,
read-only registry of archived revisions — display metadata only (names,
weights, scale, band lines); **no arithmetic is ever recomputed from it**.

Registry contents below were extracted on 2026-08-28 from git history on the
host and from the production backup dump, hash-verified:

| version | content_hash | rows in backup | source |
|---|---|---|---|
| 1.0.0 | `b3aea2c8d235` | 4 | `git show d1eae0f:prompts/rubric/blackbird-rubric.toml` (sha256[:12] matches) |
| 2.0.0 | `e3ef75f84c48` | 38 | `git show 2740b5a:…` (matches) |
| 2.1.0 | `2f38fc9bce4d` | 9 | `git show 3cdb7f5:…` (matches) |
| 2.2.0 | `1fccf33fc990` | 4 | dangling blob `git cat-file blob c067ccd38610b94786ac21292688c0e46ee8728f` (matches) |
| 3.0.0 | `5743056327df` | 5 | **document unrecoverable**; entry reconstructed from the live document's `[meta].changelog` (the 3.0.0 entry lists the six dimension keys + weights, the 3.4/2.8 bands and the 1–5 scale; 3.2.0's entry records "No weight, threshold, gating, or dimension changes", and 3.1.0's records only the funnel-stage removal, "no arithmetic role" since 3.0.0) |
| (live 3.2.0) | `42aec0479ac6` | — | never in the registry — derived from `load_rubric()` at read time |

(22 further backup rows are stamped NULL/NULL — they resolve as "unstamped".)

**Files:**
- Create: `prompts/rubric/revisions.toml`
- Create: `src/services/rubric_revisions.py`
- Test: `tests/unit/test_rubric_revisions.py`

**Interfaces:**
- Produces (consumed by Task 5):
  - `RevisionDimension(key: str, title: str, weight: int | None, weight_note: str)`
  - `RubricRevisionView(version, content_hash, scale_min, scale_max, advance_min: float | None, conditional_min: float | None, pass_label: str | None, banding_note: str | None, dimensions: tuple[RevisionDimension, ...])`
  - `resolve_revision(version: str | None, content_hash: str | None) -> tuple[RubricRevisionView | None, str]`
    with provenance constants `PROVENANCE_LIVE = "live"`,
    `PROVENANCE_ARCHIVED = "archived"`, `PROVENANCE_UNSTAMPED = "unstamped"`,
    `PROVENANCE_UNKNOWN = "unknown"`.
  - `live_revision_view() -> RubricRevisionView`
- Consumes: `load_rubric()` / `Rubric` from `src/services/blackbird_rubric.py`
  (fields verified: version, content_hash, scale_min, scale_max, advance_min,
  conditional_min, pass_label, dimensions with key/weight/title).

- [ ] **Step 1: Write the failing tests** — create
  `tests/unit/test_rubric_revisions.py`:

```python
"""The revision registry: stored assessments render against the revision that
scored them, never silently against the live document (audit A3)."""
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.rubric_revisions import (
    PROVENANCE_ARCHIVED,
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
    PROVENANCE_UNSTAMPED,
    live_revision_view,
    resolve_revision,
)


def test_the_live_view_mirrors_the_loaded_document():
    view = live_revision_view()
    assert view.version == RUBRIC_VERSION
    assert view.content_hash == RUBRIC_CONTENT_HASH
    assert view.advance_min is not None and view.conditional_min is not None
    assert all(d.weight_note == f"{d.weight}%" for d in view.dimensions)


def test_resolving_the_live_stamp_returns_the_live_view():
    view, provenance = resolve_revision(RUBRIC_VERSION, RUBRIC_CONTENT_HASH)
    assert provenance == PROVENANCE_LIVE
    assert view.content_hash == RUBRIC_CONTENT_HASH


def test_an_archived_hash_resolves_to_its_registry_entry():
    view, provenance = resolve_revision("2.1.0", "2f38fc9bce4d")
    assert provenance == PROVENANCE_ARCHIVED
    assert view.advance_min == 4.0 and view.conditional_min == 3.0
    keys = [d.key for d in view.dimensions]
    assert len(keys) == 13 and "ip_fto" in keys and "chemistry_dc_path" in keys
    ip_fto = next(d for d in view.dimensions if d.key == "ip_fto")
    assert ip_fto.weight_note == "6%/4% (investment/incubation)"
    assert view.banding_note, "dual-scale caveat must be recorded"


def test_a_version_resolves_without_a_hash_only_when_unambiguous():
    view, provenance = resolve_revision("3.0.0", None)
    assert provenance == PROVENANCE_ARCHIVED
    assert [d.key for d in view.dimensions][:2] == [
        "differentiation_unmet_need", "scientific_credibility",
    ]
    assert view.advance_min == 3.4 and view.conditional_min == 2.8


def test_an_unmatched_stamp_is_unknown_never_guessed():
    view, provenance = resolve_revision("9.9.9", "deadbeef0000")
    assert (view, provenance) == (None, PROVENANCE_UNKNOWN)
    # A known version with a WRONG hash is a different document — unknown too.
    view, provenance = resolve_revision("2.1.0", "deadbeef0000")
    assert (view, provenance) == (None, PROVENANCE_UNKNOWN)


def test_no_stamp_at_all_reads_against_the_live_document():
    view, provenance = resolve_revision(None, None)
    assert provenance == PROVENANCE_UNSTAMPED
    assert view.content_hash == RUBRIC_CONTENT_HASH


def test_registry_hashes_are_unique_and_never_shadow_the_live_document():
    from src.services.rubric_revisions import _ARCHIVED
    hashes = [v.content_hash for v in _ARCHIVED]
    assert len(hashes) == len(set(hashes))
    assert RUBRIC_CONTENT_HASH not in hashes, (
        "the live document is derived at read time, never duplicated into the registry"
    )
```

- [ ] **Step 2: Run to verify they fail** (module does not exist):

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_rubric_revisions.py -q'`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `prompts/rubric/revisions.toml`** with exactly this
  content (13-dimension tables are identical across 2.0.0/2.1.0/2.2.0,
  verified against each document):

```toml
# Archived rubric revisions — READ-SIDE DISPLAY METADATA ONLY.
#
# Each entry lets the assessment pages render a stored row against the
# revision that scored it: dimension names, weights, scale, band lines.
# Nothing is ever recomputed from this file — stored weighted_score/band are
# write-time facts. The LIVE document (blackbird-rubric.toml) is never
# duplicated here; its view is derived from load_rubric() at read time.
#
# MAINTENANCE RULE (CLAUDE.md "assessment archive" box): every time
# [meta].version is bumped in blackbird-rubric.toml, append the OUTGOING
# document's entry here in the same commit — version, sha256[:12] of the old
# file bytes, scale, band lines, and the dimension table.
#
# Provenance of the entries below (extracted + hash-verified 2026-08-28):
#   1.0.0 b3aea2c8d235  <- git d1eae0f
#   2.0.0 e3ef75f84c48  <- git 2740b5a
#   2.1.0 2f38fc9bce4d  <- git 3cdb7f5
#   2.2.0 1fccf33fc990  <- dangling blob c067ccd38610b94786ac21292688c0e46ee8728f
#   3.0.0 5743056327df  <- document unrecoverable; reconstructed from the live
#         document's [meta].changelog: the 3.0.0 entry attests the six keys,
#         weights, 3.4/2.8 bands and 1-5 scale; 3.2.0's entry records "No
#         weight, threshold, gating, or dimension changes", 3.1.0's only the
#         funnel removal ("no arithmetic role" since 3.0.0). Titles and
#         pass_label are carried over from the 3.2.0 document (not separately
#         attested) — see this entry's banding_note.

[[revision]]
version = "1.0.0"
content_hash = "b3aea2c8d235"
scale_min = 1
scale_max = 5
advance_min = 4.0
conditional_min = 3.0
pass_label = "pass (decline)"

  [[revision.dimension]]
  key = "differentiation"
  title = "Commercialization potential / differentiation"
  weight = 15
  [[revision.dimension]]
  key = "market_unmet_need"
  title = "Market size & actionable unmet need"
  weight = 12
  [[revision.dimension]]
  key = "team"
  title = "Team / founder quality"
  weight = 10
  [[revision.dimension]]
  key = "external_signals"
  title = "External signals"
  weight = 8
  [[revision.dimension]]
  key = "ip_fto"
  title = "IP position & FTO"
  weight = 6
  [[revision.dimension]]
  key = "platform"
  title = "Platform vs. single asset"
  weight = 4
  [[revision.dimension]]
  key = "dev_regulatory_feasibility"
  title = "Development & regulatory feasibility"
  weight = 3
  [[revision.dimension]]
  key = "workplan_capital_efficiency"
  title = "Work-plan feasibility & capital efficiency"
  weight = 1
  [[revision.dimension]]
  key = "exit_thesis"
  title = "Value-creation / exit thesis"
  weight = 1
  [[revision.dimension]]
  key = "mechanism_validation"
  title = "mechanism_validation"
  weight = 12
  [[revision.dimension]]
  key = "toxicity_selectivity"
  title = "toxicity_selectivity"
  weight = 10
  [[revision.dimension]]
  key = "experimental_rigor"
  title = "experimental_rigor"
  weight = 10
  [[revision.dimension]]
  key = "chemistry_dc_path"
  title = "chemistry_dc_path"
  weight = 8

[[revision]]
version = "2.0.0"
content_hash = "e3ef75f84c48"
scale_min = 1
scale_max = 5
advance_min = 4.0
conditional_min = 3.0
pass_label = "pass (decline)"
banding_note = "Dual-scale document: the incubation arm banded at 3.4/2.7 with its own weights; which arm scored a given row was stage-selected at write time and is not recorded on the row. Investment-arm lines shown."

  [[revision.dimension]]
  key = "differentiation"
  title = "Commercialization potential / differentiation"
  weight = 15
  weight_incubation = 16
  [[revision.dimension]]
  key = "market_unmet_need"
  title = "Market size & actionable unmet need"
  weight = 12
  weight_incubation = 14
  [[revision.dimension]]
  key = "team"
  title = "Team / founder quality"
  weight = 10
  weight_incubation = 12
  [[revision.dimension]]
  key = "external_signals"
  title = "External signals"
  weight = 8
  weight_incubation = 2
  [[revision.dimension]]
  key = "ip_fto"
  title = "IP position & FTO"
  weight = 6
  weight_incubation = 4
  [[revision.dimension]]
  key = "platform"
  title = "Platform vs. single asset"
  weight = 4
  weight_incubation = 5
  [[revision.dimension]]
  key = "dev_regulatory_feasibility"
  title = "Development & regulatory feasibility"
  weight = 3
  weight_incubation = 3
  [[revision.dimension]]
  key = "workplan_capital_efficiency"
  title = "Work-plan feasibility & capital efficiency"
  weight = 1
  weight_incubation = 8
  [[revision.dimension]]
  key = "exit_thesis"
  title = "Value-creation / exit thesis"
  weight = 1
  weight_incubation = 2
  [[revision.dimension]]
  key = "mechanism_validation"
  title = "mechanism_validation"
  weight = 12
  weight_incubation = 10
  [[revision.dimension]]
  key = "toxicity_selectivity"
  title = "toxicity_selectivity"
  weight = 10
  weight_incubation = 8
  [[revision.dimension]]
  key = "experimental_rigor"
  title = "experimental_rigor"
  weight = 10
  weight_incubation = 8
  [[revision.dimension]]
  key = "chemistry_dc_path"
  title = "chemistry_dc_path"
  weight = 8
  weight_incubation = 8

[[revision]]
version = "2.1.0"
content_hash = "2f38fc9bce4d"
scale_min = 1
scale_max = 5
advance_min = 4.0
conditional_min = 3.0
pass_label = "pass (decline)"
banding_note = "Dual-scale document: the incubation arm banded at 3.4/2.7 with its own weights; which arm scored a given row was stage-selected at write time and is not recorded on the row. Investment-arm lines shown."

  [[revision.dimension]]
  key = "differentiation"
  title = "Commercialization potential / differentiation"
  weight = 15
  weight_incubation = 16
  [[revision.dimension]]
  key = "market_unmet_need"
  title = "Market size & actionable unmet need"
  weight = 12
  weight_incubation = 14
  [[revision.dimension]]
  key = "team"
  title = "Team / founder quality"
  weight = 10
  weight_incubation = 12
  [[revision.dimension]]
  key = "external_signals"
  title = "External signals"
  weight = 8
  weight_incubation = 2
  [[revision.dimension]]
  key = "ip_fto"
  title = "IP position & FTO"
  weight = 6
  weight_incubation = 4
  [[revision.dimension]]
  key = "platform"
  title = "Platform vs. single asset"
  weight = 4
  weight_incubation = 5
  [[revision.dimension]]
  key = "dev_regulatory_feasibility"
  title = "Development & regulatory feasibility"
  weight = 3
  weight_incubation = 3
  [[revision.dimension]]
  key = "workplan_capital_efficiency"
  title = "Work-plan feasibility & capital efficiency"
  weight = 1
  weight_incubation = 8
  [[revision.dimension]]
  key = "exit_thesis"
  title = "Value-creation / exit thesis"
  weight = 1
  weight_incubation = 2
  [[revision.dimension]]
  key = "mechanism_validation"
  title = "mechanism_validation"
  weight = 12
  weight_incubation = 10
  [[revision.dimension]]
  key = "toxicity_selectivity"
  title = "toxicity_selectivity"
  weight = 10
  weight_incubation = 8
  [[revision.dimension]]
  key = "experimental_rigor"
  title = "experimental_rigor"
  weight = 10
  weight_incubation = 8
  [[revision.dimension]]
  key = "chemistry_dc_path"
  title = "chemistry_dc_path"
  weight = 8
  weight_incubation = 8

[[revision]]
version = "2.2.0"
content_hash = "1fccf33fc990"
scale_min = 1
scale_max = 5
advance_min = 4.0
conditional_min = 3.0
pass_label = "pass (decline)"
banding_note = "Dual-scale document: the incubation arm banded at 3.4/2.7 with its own weights; which arm scored a given row was stage-selected at write time and is not recorded on the row. Investment-arm lines shown."

  [[revision.dimension]]
  key = "differentiation"
  title = "Commercialization potential / differentiation"
  weight = 15
  weight_incubation = 16
  [[revision.dimension]]
  key = "market_unmet_need"
  title = "Market size & actionable unmet need"
  weight = 12
  weight_incubation = 14
  [[revision.dimension]]
  key = "team"
  title = "Team / founder quality"
  weight = 10
  weight_incubation = 12
  [[revision.dimension]]
  key = "external_signals"
  title = "External signals"
  weight = 8
  weight_incubation = 2
  [[revision.dimension]]
  key = "ip_fto"
  title = "IP position & FTO"
  weight = 6
  weight_incubation = 4
  [[revision.dimension]]
  key = "platform"
  title = "Platform vs. single asset"
  weight = 4
  weight_incubation = 5
  [[revision.dimension]]
  key = "dev_regulatory_feasibility"
  title = "Development & regulatory feasibility"
  weight = 3
  weight_incubation = 3
  [[revision.dimension]]
  key = "workplan_capital_efficiency"
  title = "Work-plan feasibility & capital efficiency"
  weight = 1
  weight_incubation = 8
  [[revision.dimension]]
  key = "exit_thesis"
  title = "Value-creation / exit thesis"
  weight = 1
  weight_incubation = 2
  [[revision.dimension]]
  key = "mechanism_validation"
  title = "mechanism_validation"
  weight = 12
  weight_incubation = 10
  [[revision.dimension]]
  key = "toxicity_selectivity"
  title = "toxicity_selectivity"
  weight = 10
  weight_incubation = 8
  [[revision.dimension]]
  key = "experimental_rigor"
  title = "experimental_rigor"
  weight = 10
  weight_incubation = 8
  [[revision.dimension]]
  key = "chemistry_dc_path"
  title = "chemistry_dc_path"
  weight = 8
  weight_incubation = 8

[[revision]]
version = "3.0.0"
content_hash = "5743056327df"
scale_min = 1
scale_max = 5
advance_min = 3.4
conditional_min = 2.8
pass_label = "pass (decline)"
banding_note = "Document not recoverable; keys, weights, bands and scale attested by the [meta].changelog (3.1.0/3.2.0 changed no weights, thresholds or dimensions); dimension titles and pass_label carried over from the 3.2.0 document."

  [[revision.dimension]]
  key = "differentiation_unmet_need"
  title = "Differentiation & unmet need"
  weight = 25
  [[revision.dimension]]
  key = "scientific_credibility"
  title = "Scientific credibility & mechanism"
  weight = 20
  [[revision.dimension]]
  key = "translational_path"
  title = "Translational & development path"
  weight = 15
  [[revision.dimension]]
  key = "fundable_experiment"
  title = "Fundable killer experiment & capital efficiency"
  weight = 15
  [[revision.dimension]]
  key = "venture_potential"
  title = "Venture potential: IP path, platform & signals"
  weight = 15
  [[revision.dimension]]
  key = "team_executability"
  title = "Team & executability"
  weight = 10
```

- [ ] **Step 4: Create `src/services/rubric_revisions.py`:**

```python
"""Read-side registry of ARCHIVED rubric revisions.

Stored assessments are stamped (rubric_version, rubric_content_hash) at write
time. The live document churns; this registry lets the read paths render each
row against the revision that scored it — dimension names, weights, scale and
band lines are DISPLAY metadata only, never re-fed into any arithmetic
(stored weighted_score/band are write-time facts and stay untouched).

The live document is never duplicated here: its view is derived from
load_rubric() so the two cannot drift. Loaded once at import, fail-fast on an
invalid document, exactly like blackbird_rubric.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.services.blackbird_rubric import load_rubric

REVISIONS_PATH = Path("prompts/rubric/revisions.toml")

PROVENANCE_LIVE = "live"
PROVENANCE_ARCHIVED = "archived"
PROVENANCE_UNSTAMPED = "unstamped"
PROVENANCE_UNKNOWN = "unknown"


class RevisionRegistryError(RuntimeError):
    """The registry document is malformed. Raised at import — a wrong registry
    must fail the deploy, not mislabel archived verdicts at 2am."""


@dataclass(frozen=True)
class RevisionDimension:
    key: str
    title: str
    weight: int | None  # numeric single-scale weight (bar shading); None for row-extras
    weight_note: str    # display string: "25%" or "6%/4% (investment/incubation)"


@dataclass(frozen=True)
class RubricRevisionView:
    version: str
    content_hash: str
    scale_min: int
    scale_max: int
    advance_min: float | None
    conditional_min: float | None
    pass_label: str | None
    banding_note: str | None
    dimensions: tuple[RevisionDimension, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RevisionRegistryError(message)


def _parse_registry(path: Path) -> tuple[RubricRevisionView, ...]:
    _require(path.is_file(), f"revision registry missing: {path}")
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    entries = doc.get("revision")
    _require(isinstance(entries, list) and entries, "[[revision]] entries required")

    views: list[RubricRevisionView] = []
    for i, raw in enumerate(entries):
        where = f"revision[{i}]"
        version = raw.get("version")
        content_hash = raw.get("content_hash")
        _require(isinstance(version, str) and version, f"{where}.version")
        _require(
            isinstance(content_hash, str) and len(content_hash) == 12,
            f"{where}.content_hash must be the 12-hex sha256 prefix",
        )
        dims_raw = raw.get("dimension")
        _require(isinstance(dims_raw, list) and dims_raw, f"{where}.dimension table required")
        dims: list[RevisionDimension] = []
        for j, d in enumerate(dims_raw):
            dwhere = f"{where}.dimension[{j}]"
            key, title, weight = d.get("key"), d.get("title"), d.get("weight")
            _require(isinstance(key, str) and key, f"{dwhere}.key")
            _require(isinstance(title, str) and title, f"{dwhere}.title")
            _require(isinstance(weight, int), f"{dwhere}.weight must be an int")
            weight_incubation = d.get("weight_incubation")
            if weight_incubation is not None:
                _require(isinstance(weight_incubation, int), f"{dwhere}.weight_incubation")
                note = f"{weight}%/{weight_incubation}% (investment/incubation)"
            else:
                note = f"{weight}%"
            dims.append(RevisionDimension(key=key, title=title, weight=weight, weight_note=note))
        _require(
            len({d.key for d in dims}) == len(dims), f"{where}: duplicate dimension keys"
        )
        views.append(RubricRevisionView(
            version=version,
            content_hash=content_hash,
            scale_min=int(raw.get("scale_min", 1)),
            scale_max=int(raw["scale_max"]) if "scale_max" in raw else 5,
            advance_min=float(raw["advance_min"]) if "advance_min" in raw else None,
            conditional_min=float(raw["conditional_min"]) if "conditional_min" in raw else None,
            pass_label=raw.get("pass_label"),
            banding_note=raw.get("banding_note"),
            dimensions=tuple(dims),
        ))
    hashes = [v.content_hash for v in views]
    _require(len(hashes) == len(set(hashes)), "duplicate content_hash entries")
    return tuple(views)


_ARCHIVED: tuple[RubricRevisionView, ...] = _parse_registry(REVISIONS_PATH)
_BY_HASH: dict[str, RubricRevisionView] = {v.content_hash: v for v in _ARCHIVED}


def live_revision_view() -> RubricRevisionView:
    r = load_rubric()
    return RubricRevisionView(
        version=r.version,
        content_hash=r.content_hash,
        scale_min=r.scale_min,
        scale_max=r.scale_max,
        advance_min=r.advance_min,
        conditional_min=r.conditional_min,
        pass_label=r.pass_label,
        banding_note=None,
        dimensions=tuple(
            RevisionDimension(key=d.key, title=d.title, weight=d.weight,
                              weight_note=f"{d.weight}%")
            for d in r.dimensions
        ),
    )


def resolve_revision(
    version: str | None, content_hash: str | None
) -> tuple[RubricRevisionView | None, str]:
    """Which revision scored a row, and how sure we are.

    Hash is authoritative: a known version with a WRONG hash is a different
    document and resolves UNKNOWN rather than to the version's registry entry.
    Version-only matching (rows written when only the version was stamped, or
    hand-built fixtures) is honoured only when exactly one candidate exists.
    """
    live = live_revision_view()
    if not version and not content_hash:
        return live, PROVENANCE_UNSTAMPED
    if content_hash:
        if content_hash == live.content_hash:
            return live, PROVENANCE_LIVE
        hit = _BY_HASH.get(content_hash)
        if hit is not None:
            return hit, PROVENANCE_ARCHIVED
        return None, PROVENANCE_UNKNOWN
    # version set, hash absent
    candidates = [v for v in _ARCHIVED if v.version == version]
    if version == live.version:
        candidates.append(live)
    if len(candidates) == 1:
        view = candidates[0]
        provenance = PROVENANCE_LIVE if view.content_hash == live.content_hash else PROVENANCE_ARCHIVED
        return view, provenance
    return None, PROVENANCE_UNKNOWN
```

- [ ] **Step 5: Run the tests**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_rubric_revisions.py -q'`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add prompts/rubric/revisions.toml src/services/rubric_revisions.py tests/unit/test_rubric_revisions.py
git commit -m "feat(assessments): rubric revision registry for version-aware reads

Display metadata (names/weights/scale/band lines) for the five archived
rubric revisions that stamped stored rows, hash-verified against git history
and the pre-purge backup. The live document is derived, never duplicated.
Audit A3/R4.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Detail page renders each row against its own revision

Today `build_assessment_detail` (`src/services/assessment_detail.py:491`)
iterates the LIVE `RUBRIC_WEIGHTS` (~:531), so a 13-dimension v2 row renders
as six blank live dimensions under live weights, beside a live 3.4/2.8 legend
contradicting its stored 4.0/3.0-banded score (audit A3/A4-hazards). After
this task: stamped rows resolve through `resolve_revision`; unmatched stamps
render the row's own score keys with an explicit warning; **unstamped rows
keep today's live-document rendering** (the existing banner already discloses
it) plus any off-document score keys appended.

**Files:**
- Modify: `src/services/assessment_detail.py` (~:523-:552 block and the
  return-dict keys at ~:620-:627)
- Modify: `templates/admin/_assessment_detail_body.html` (threshold legend
  ~:149-:153, revision banner ~:157-:173, dimension list ~:329-:356)
- Test: `tests/integration/test_assessment_detail_page.py` (extend)

**Interfaces:**
- Consumes (Task 4): `resolve_revision`, provenance constants.
- Produces template context: `dimensions` items become
  `{key, title, weight: int|None, weight_note: str|None, score, pct: float|None}`;
  new keys `revision` (RubricRevisionView | None) and
  `revision_provenance` (str); `scale_max` becomes `int | None`;
  `rubric_weights` is dropped (grep-verified unused by templates);
  `banding`/`rubric_version` stay (live pass-label vocabulary + banner
  comparison).

- [ ] **Step 1: Write the failing tests** (append to
  `tests/integration/test_assessment_detail_page.py`):

```python
async def _seed_stamped(db_session, *, version, content_hash, scores):
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT,
        channel_name=CHANNEL, company_or_project="Stamped Fixture Co",
        recommendation="conditional", weighted_score=3.20, band="conditional",
        rubric_version=version, rubric_content_hash=content_hash, scores=scores,
    )
    db_session.add(assessment)
    await db_session.flush()
    return assessment


async def test_an_archived_stamp_renders_that_revisions_dimensions(
    client, db_session, admin
):
    """A v2.1.0 row must show its own 13-dimension space and 4.0/3.0 band
    lines — not six blank live dimensions under a 3.4/2.8 legend."""
    assessment = await _seed_stamped(
        db_session, version="2.1.0", content_hash="2f38fc9bce4d",
        scores={"ip_fto": 3, "differentiation": 4},
    )
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert 'class="score-ip_fto' in html
    assert "6%/4% (investment/incubation)" in html
    assert "&ge;4.0 advance" in html and "&lt;3.0" in html
    assert 'class="score-scientific_credibility' not in html, (
        "live-document dimensions leaked into an archived row's render"
    )


async def test_an_unknown_stamp_renders_the_rows_own_scores_with_a_warning(
    client, db_session, admin
):
    assessment = await _seed_stamped(
        db_session, version="9.9.9", content_hash="deadbeef0000",
        scores={"mystery_dim": 2},
    )
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert 'class="score-mystery_dim' in html
    assert "matches no entry in the revision registry" in html
    assert 'class="score-differentiation_unmet_need' not in html


async def test_an_unstamped_row_keeps_the_live_render_and_shows_extras(
    client, db_session, admin
):
    """Pre-0030 rows have no stamp: they keep today's live-document rendering
    (the banner already says so) — but any score key the live document does
    not name must still render instead of vanishing."""
    assessment = await _seed_stamped(
        db_session, version=None, content_hash=None,
        scores={"differentiation_unmet_need": 4, "ip_fto": 3},
    )
    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert 'class="score-differentiation_unmet_need' in html
    assert 'class="score-scientific_credibility' in html  # live dims all listed
    assert 'class="score-ip_fto' in html                  # extra appended
```

- [ ] **Step 2: Run to verify the three fail** (live-key rendering today):

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -q'`
Expected: the three new tests FAIL; all pre-existing tests PASS.

- [ ] **Step 3: Rewrite the dimension-building block** in
  `build_assessment_detail` (anchor: `rubric = load_rubric()` at ~:525 through
  the `dimensions.append({...})` loop ending ~:550) with:

```python
    revision, revision_provenance = resolve_revision(
        assessment.rubric_version, assessment.rubric_content_hash
    )
    scores = assessment.scores if isinstance(assessment.scores, dict) else {}
    normalized_scores = {
        key.strip().lower(): value
        for key, value in scores.items()
        if isinstance(key, str)
    }

    def _score_value(raw: object) -> float | None:
        return (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )

    def _pct(value: float | None) -> float | None:
        # Bar width as a percentage of the revision's scale, clamped — a
        # verdict can carry an out-of-range score and a >100% width would
        # overflow the track. No revision -> no known scale -> no bar.
        if revision is None:
            return None
        if value is None:
            return 0.0
        return min(100.0, max(0.0, value / revision.scale_max * 100.0))

    dimensions = []
    named_keys: set[str] = set()
    if revision is not None:
        for dim in revision.dimensions:
            value = _score_value(normalized_scores.get(dim.key))
            named_keys.add(dim.key)
            dimensions.append({
                "key": dim.key,
                "title": dim.title,
                "weight": dim.weight,
                "weight_note": dim.weight_note,
                "score": value,
                "pct": _pct(value),
            })
    # Score keys the chosen revision does not name still render — a stored row
    # must show its data, never blanks (the pre-registry page dropped a v2
    # row's 13 scores on the floor).
    for key in sorted(normalized_scores):
        if key in named_keys:
            continue
        value = _score_value(normalized_scores[key])
        if value is None:
            continue
        dimensions.append({
            "key": key,
            "title": key.replace("_", " "),
            "weight": None,
            "weight_note": None,
            "score": value,
            "pct": _pct(value),
        })
```

  Add the import
  `from src.services.rubric_revisions import resolve_revision` next to the
  existing blackbird_rubric imports (~:50-:56), delete the now-unused local
  `rubric = load_rubric()` (and the `load_rubric` import if nothing else in
  the module uses it — grep first), and update the return dict (anchor
  `"scale_max": rubric.scale_max,`):

```python
        "dimensions": dimensions,
        "revision": revision,
        "revision_provenance": revision_provenance,
        "scale_max": revision.scale_max if revision is not None else None,
        "banding": BANDING,
        "rubric_version": RUBRIC_VERSION,
```

  (`"rubric_weights": RUBRIC_WEIGHTS,` is removed; grep
  `templates/ -rn rubric_weights` first to re-confirm it is unused, and drop
  the `RUBRIC_WEIGHTS` import if the module no longer needs it.)

- [ ] **Step 4: Update `templates/admin/_assessment_detail_body.html`** (three
  edits, anchored by quoted current content):

  (a) Threshold legend (anchor: `computed, not taken from the model &middot;`):

```jinja
{# BEFORE #}
            <div class="text-xs text-gray-400 mt-1">
                computed, not taken from the model &middot;
                &ge;{{ banding.advance_min }} advance,
                &lt;{{ banding.conditional_min }} {{ banding.pass_label }}
            </div>
{# AFTER #}
            <div class="text-xs text-gray-400 mt-1">
                computed, not taken from the model &middot;
                {% if revision and revision.advance_min is not none %}
                    &ge;{{ revision.advance_min }} advance,
                    &lt;{{ revision.conditional_min }} {{ revision.pass_label or banding.pass_label }}{% if revision.banding_note %}
                    <span class="cursor-help" title="{{ revision.banding_note }}">*</span>{% endif %}
                {% else %}
                    band lines for this row's revision are not recorded
                {% endif %}
            </div>
```

  (b) Dimension rows + scale caption (anchor: `{% for d in dimensions %}`
  through the `Bars are the score on the 1&ndash;{{ scale_max }} scale.`
  paragraph): replace the loop body's three spots that assume `d.weight` is a
  number and `d.pct` exists —

```jinja
{# label span: #}
                {{ d.title }}
{# bar cell: #}
            <span class="flex-1 h-2 bg-gray-100 rounded-full overflow-hidden" title="{{ d.title }}{% if d.weight_note %} — {{ d.weight_note }} of the weighted score{% endif %}{% if d.score is none %} (not scored: counts as zero){% endif %}">
                {% if d.pct is not none %}
                <span class="block h-2 rounded-full {% if d.weight and d.weight >= 10 %}bg-indigo-500{% else %}bg-indigo-300{% endif %}" style="width: {{ "%.0f"|format(d.pct) }}%"></span>
                {% endif %}
            </span>
            <span class="w-24 shrink-0 text-gray-400">
                {% if d.weight_note %}{{ d.weight_note }} weight{% else %}&mdash;{% endif %}{% if d.score is none %} &middot; not scored{% endif %}
            </span>
```

  and the caption:

```jinja
    <p class="text-xs text-gray-400 mt-3">
        {% if scale_max %}Bars are the score on the {{ revision.scale_min }}&ndash;{{ scale_max }} scale.{% else %}This row's revision is not in the registry, so its scale is unknown and no bars are drawn.{% endif %}
        The weighted score above was computed at write time from these dimension
        values and the weights of the revision that scored them
        (src/services/blackbird_rubric.py) — an unscored dimension counted as zero.
    </p>
```

  Keep the `score-{{ d.key }}` class and the score-number cell exactly as they
  are (tests pin the class names and the "not scored" text). Also update the
  `{# … #}` block comment above the dimension list (~:322-:328): it says the
  bars render "in RUBRIC_WEIGHTS order" on "the 1-{{ scale_max }} scale" —
  after this task the order comes from the resolved revision (plus row-extras)
  and the scale may be unknown; reword it accordingly (it is a Jinja comment,
  so this is prose-only, no render risk either way).

  (c) Revision banner (anchor: `Scored against rubric` block): after the
  existing mismatch warning `{% endif %}`, still inside the
  `{% if a.rubric_version %}` branch, add:

```jinja
            {% if revision_provenance == 'archived' %}
                <span class="text-gray-500">Dimension names, weights and band lines above
                are that revision's, from prompts/rubric/revisions.toml.</span>
            {% elif revision_provenance == 'unknown' %}
                <span class="text-amber-700">This stamp matches no entry in the revision registry —
                scores are shown as stored, with no weights or band lines.</span>
            {% endif %}
```

  Keep the phrase `matches no entry in the revision registry` on ONE source
  line — Jinja renders literal text verbatim (no trim/lstrip in this app's
  Starlette-default environment), so a mid-phrase line break would put a
  newline + indent inside the sentence and the Step-1 substring assertion
  could never match. (Cosmetic residual, fine to leave: a row with a
  `rubric_content_hash` but NULL `rubric_version` falls into the existing
  `{% else %}` "predates stamping" branch — the columns are independently
  nullable but no such row exists in the corpus or the backup.)

- [ ] **Step 5: Run the whole detail-page suite (old tests are the regression
  net: the 3.0.0-stamped `test_detail_page_renders_the_documents_banding_and_weights`
  must now pass via the registry's 3.0.0 entry, and the unstamped `_seed` tests
  via the live fallback):**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py tests/unit/test_panel_state.py tests/unit/test_assessment_timeline.py -q'`
Expected: PASS. Two follow-ups while here: (1)
`test_detail_page_renders_the_documents_banding_and_weights` now resolves its
`rubric_version="3.0.0"` fixture through the ARCHIVED registry entry rather
than the live document (its four assertions still pass because 3.0.0's numbers
equal the live ones) — update its docstring to say "the row's own revision
entry", since live-legend coverage now lives in
`test_the_assessments_legend_states_the_rubric_thresholds`. (2) Confirm the
now-dead `load_rubric` and `RUBRIC_WEIGHTS` imports were removed from
`assessment_detail.py` (verified: after this task nothing else in the module
uses them, and F401 fails the tests/src gate).

- [ ] **Step 6: Commit**

```bash
git add src/services/assessment_detail.py templates/admin/_assessment_detail_body.html tests/integration/test_assessment_detail_page.py
git commit -m "feat(assessments): render each verdict against the revision that scored it

Stamped rows resolve through the revision registry; unmatched stamps render
the row's own scores with an explicit warning; unstamped rows keep the live
render (disclosed) plus off-document extras. Fixes the blank-dimensions /
wrong-legend misrender of archived rows. Audit A3.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: List pages — provenance column, honest aggregates, honest copy

Three small honesty fixes on `/admin/assessments` + `/manager/assessments`
(audit A2/A4/A6-hazards): a per-row Rubric column; per-run stored-row counts
(and the run's stamped rubric, Task 7) in the run dropdown; a note when the
aggregate tables silently exclude off-revision rows; and the now-false
"never deleted" copy corrected. **No aggregate arithmetic changes.**

**Files:**
- Modify: `src/services/directory.py` (`list_assessments`: two new view keys)
- Modify: `templates/admin/_assessments_body.html` (Rubric column, aggregates
  note, empty-state text)
- Modify: `templates/admin/assessments.html`,
  `templates/manager/assessments.html` (dropdown labels; retention sentence)
- Modify: `src/routers/admin.py` (`admin_assessments` explicit-allowlist
  forwarding — the manager route splats `**view` and needs nothing)
- Test: `tests/unit/test_directory_assessments.py` (extend)

**Interfaces:**
- Produces view keys: `assessment_counts_by_run: dict[uuid.UUID, int]`
  (stored rows per run, unfiltered by the page's run selection) and
  `off_rubric_count: int` (displayed rows whose non-empty `scores` share no
  key with the live document).
- Consumes: `RUBRIC_WEIGHTS` (already imported in `directory.py`),
  `OpportunityAssessment`, `func` (already imported).

- [ ] **Step 1: Write the failing tests** (append to
  `tests/unit/test_directory_assessments.py`, matching its
  `@pytest.mark.asyncio` + `db_session` + direct-model style):

```python
@pytest.mark.asyncio
async def test_the_view_counts_stored_rows_per_run(db_session):
    """The run dropdown must distinguish an empty run from a populated one —
    'No assessments recorded yet.' used to be the same string for both."""
    run_a = await factories.make_simulation_run(db_session)
    run_b = await factories.make_simulation_run(db_session)
    for _ in range(2):
        db_session.add(OpportunityAssessment(
            simulation_run_id=run_a.id, agent_id="blackbird",
            channel_name="general", recommendation="pass",
        ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run_b.id, agent_id="blackbird",
        channel_name="general", recommendation="pass",
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run_a.id))
    assert view["assessment_counts_by_run"][run_a.id] == 2
    assert view["assessment_counts_by_run"][run_b.id] == 1


@pytest.mark.asyncio
async def test_off_rubric_rows_are_counted_not_silently_dropped(db_session):
    """dimension_stats picks values by live key, so an archived-revision row
    contributes nothing — the page must SAY so instead of looking authoritative
    over a corpus it ignored."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass", rubric_version="2.1.0",
        rubric_content_hash="2f38fc9bce4d", scores={"ip_fto": 3},
    ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass", scores={"differentiation_unmet_need": 4},
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert view["off_rubric_count"] == 1
```

- [ ] **Step 2: Run to verify they fail** (KeyError on the new view keys):

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_directory_assessments.py -q'`
Expected: the two new tests FAIL with `KeyError`; the rest PASS.

- [ ] **Step 3: Add the two computations** in `list_assessments`
  (`src/services/directory.py`), after its `runs` query (anchor:
  `runs = runs_result.scalars().all()` **inside `list_assessments`, ~:271 —
  the identical line also appears in two other functions in this module, so
  match within the function, not file-wide**):

```python
    # Stored rows per run — the dropdown's honesty device: an old run showing
    # "0 stored" is distinguishable from a populated one, and (post-purge) from
    # a run whose rows exist only in the offline backup.
    counts_result = await db.execute(
        select(OpportunityAssessment.simulation_run_id, func.count())
        .group_by(OpportunityAssessment.simulation_run_id)
    )
    assessment_counts_by_run = dict(counts_result.all())
```

  and after the `band_counts` computation (anchor:
  `band_counts = sorted(Counter(`):

```python
    # Rows whose scores share no key with the live document contribute n=0 to
    # every dimension_stats row and pool their bands from another threshold
    # regime — count them so the tables can disclose what they exclude.
    live_keys = set(RUBRIC_WEIGHTS)
    off_rubric_count = sum(
        1
        for row in assessments
        if isinstance(row.scores, dict)
        and row.scores
        and not (live_keys & set(row.scores))
    )
```

  and the two view keys next to `"dimension_stats"`/`"band_counts"`:

```python
        "assessment_counts_by_run": assessment_counts_by_run,
        "off_rubric_count": off_rubric_count,
```

- [ ] **Step 4: Forward through the admin allowlist** — in
  `src/routers/admin.py::admin_assessments` (anchor:
  `band_counts=view["band_counts"],`) add:

```python
            assessment_counts_by_run=view["assessment_counts_by_run"],
            off_rubric_count=view["off_rubric_count"],
```

- [ ] **Step 5: Template edits.**

  (a) Both dropdowns — `templates/admin/assessments.html` and
  `templates/manager/assessments.html` (anchor: the `<option value="{{ run.id }}"`
  label line):

```jinja
{# BEFORE #}
                {{ run.started_at.strftime('%b %d %H:%M') }} ({{ run.status }}){% if run.id == runs[0].id %} — current{% endif %}
{# AFTER #}
                {{ run.started_at.strftime('%b %d %H:%M') }} ({{ run.status }}, {{ assessment_counts_by_run.get(run.id, 0) }} stored{% if run.config and run.config.get('rubric_version') %}, rubric {{ run.config.get('rubric_version') }}{% endif %}){% if run.id == runs[0].id %} — current{% endif %}
```

  (b) Retention sentence, admin (anchor: `runs still exist and are never
  deleted;`):

```jinja
        Showing the current run only — verdict rows survive <code>--fresh</code>
        restarts and nothing in the app deletes them (the one operator purge on
        record, 2026-08-27, was backed up first); pick a run above or
        <a class="underline" href="/admin/assessments?run_id=all">view all runs</a>.
```

  Manager twin (anchor: `assessments from earlier runs still exist` /
  `and are never deleted; pick a run above or`): same replacement wording with
  the `/manager/assessments?run_id=all` href.

  Also correct the same claim at its third site, the service docstring —
  `src/services/directory.py` `list_assessments` docstring lines "nothing is
  ever deleted from this view" / "NEVER ``opportunity_assessments`` — a
  screening verdict is a durable record" (~:256-:261): append "(one
  operator-run, backed-up purge on record: 2026-08-27, rubric v3)".

  (c) Rubric column in `templates/admin/_assessments_body.html`: add a header
  cell after the `Red flags` `<th>` (anchor:
  `<th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Red flags</th>`):

```jinja
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Rubric</th>
```

  and the row cell after the red-flags `</td>` (anchor: the
  `{% if a.red_flags %}` cell's closing `</td>`):

```jinja
                <td class="px-4 py-3 text-xs">
                    {% if a.rubric_version %}
                        <span class="font-mono text-gray-600"{% if a.rubric_content_hash %} title="content hash {{ a.rubric_content_hash }}"{% endif %}>{{ a.rubric_version }}</span>
                    {% else %}
                        <span class="text-gray-400" title="written before rubric stamping (migration 0030)">&mdash;</span>
                    {% endif %}
                </td>
```

  (d) Aggregates note — inside the `Dimension distribution` `<details>` block,
  right after the `</summary>` line (anchor: `</summary>`, which occurs
  exactly once in the file; "Dimension distribution" also appears in a Jinja
  comment and is NOT unique):

```jinja
    {% if off_rubric_count %}
    <p class="mt-2 text-xs text-amber-700">
        {{ off_rubric_count }} displayed row{{ '' if off_rubric_count == 1 else 's' }}
        carr{{ 'ies' if off_rubric_count == 1 else 'y' }} scores from another rubric
        revision and contribute nothing to this table (and pool
        differently-thresholded bands above) — compare across revisions on the
        detail pages, not here.
    </p>
    {% endif %}
```

  (e) Empty state (anchor: `No assessments recorded yet.`):

```jinja
    No assessments stored for {{ 'any run' if show_all_runs else 'this run' }} —
    the run menu shows each run's stored count.
```

- [ ] **Step 6: Run the page suites**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/unit/test_directory_assessments.py tests/unit/test_directory_assessment_sorting.py tests/integration/test_manager_views.py tests/integration/test_assessment_pi_link_rendering.py tests/integration/test_assessment_detail_page.py -q'`
Expected: PASS. (These integration suites render both pages end-to-end, so a
Jinja error in any edited template fails loudly here. If a test asserts the
old empty-state or retention strings, update that assertion — the strings were
factually wrong.)

- [ ] **Step 7: Commit**

```bash
git add src/services/directory.py src/routers/admin.py templates/admin/_assessments_body.html templates/admin/assessments.html templates/manager/assessments.html tests/unit/test_directory_assessments.py
git commit -m "feat(assessments): run/rubric provenance on the list pages

Per-row rubric stamp column, per-run stored counts (+ stamped rubric) in the
run dropdown, an exclusion note on the live-keyed aggregate tables, and the
post-purge correction of the 'never deleted' copy. No aggregate arithmetic
changes. Audit A2/A4.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Stamp each run with the rubric that opened it

`simulation_runs.config` carries six scheduler knobs and no version identity
(audit A5); the rubric version/hash exist only in the startup log banner
(`src/agent/main.py:345`, from `RUBRIC_VERSION`/`RUBRIC_CONTENT_HASH`, already
imported at main.py:22). Stamp them into `config` at run creation — the
dropdown label added in Task 6 then displays them. `config` is a plain JSON
column; **no migration**.

**Files:**
- Modify: `src/agent/main.py` (`_open_fresh_run`, the resume/else-create
  branch in `_run_simulation`)
- Test: `tests/integration/test_run_config_stamp.py` (new)

**Interfaces:**
- Produces: every `SimulationRun` row created from `main.py` carries
  `config["rubric_version"]` and `config["rubric_content_hash"]`; resuming a
  run stamped with a DIFFERENT hash logs one WARNING (per-assessment stamps
  stay authoritative — a mid-run rubric change across restarts is already
  visible per row).

- [ ] **Step 1: Write the failing test** — create
  `tests/integration/test_run_config_stamp.py`:

```python
"""A run row must say which rubric opened it: with pre-v3 assessment rows
purged, nothing in the DB could name a run's rubric at all (audit A5)."""
import pytest
from sqlalchemy import select

from src.agent.main import _open_fresh_run
from src.models import SimulationRun
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION

pytestmark = pytest.mark.integration


class _FixtureSessionFactory:
    """Same shim as test_state_rebuild.py: route the engine-owned session at
    the test's rolled-back session; __aexit__ must not close it."""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


async def test_open_fresh_run_stamps_the_rubric(db_session):
    run_id = await _open_fresh_run(
        _FixtureSessionFactory(db_session), {"max_runtime": 0}
    )
    run = (
        await db_session.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )
    ).scalar_one()
    assert run.config["rubric_version"] == RUBRIC_VERSION
    assert run.config["rubric_content_hash"] == RUBRIC_CONTENT_HASH
    assert run.config["max_runtime"] == 0, "caller's config keys must survive"
```

- [ ] **Step 2: Run to verify it fails**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_run_config_stamp.py -q'`
Expected: FAIL with `KeyError: 'rubric_version'`.

- [ ] **Step 3: Implement.** In `src/agent/main.py`:

  (a) Add a module-level helper above `_open_fresh_run`:

```python
def _stamp_run_config(config: dict) -> dict:
    """The run row's own record of which rubric opened it. The startup banner
    already logs these two values; persisting them is what lets the admin run
    dropdown label a run by rubric after the log is gone. Per-assessment
    stamps stay authoritative for individual verdicts."""
    return {
        **config,
        "rubric_version": RUBRIC_VERSION,
        "rubric_content_hash": RUBRIC_CONTENT_HASH,
    }
```

  (b) In `_open_fresh_run` (anchor:
  `run = SimulationRun(status="running", config=dict(config))`):

```python
        run = SimulationRun(status="running", config=_stamp_run_config(config))
```

  (c) In the resume/else-create branch of `_run_simulation`: the else-create
  site (anchor: `run = SimulationRun(status="running", config=run_config)`)
  becomes `config=_stamp_run_config(run_config)`; and in the resume branch
  (anchor: `existing_run.status = "running"`), add before it:

```python
                    stamped_hash = (existing_run.config or {}).get("rubric_content_hash")
                    if stamped_hash and stamped_hash != RUBRIC_CONTENT_HASH:
                        logger.warning(
                            "Resuming run %s, which opened under rubric %s (%s); "
                            "this process loaded %s (%s). Per-assessment stamps "
                            "remain authoritative.",
                            existing_run.id,
                            (existing_run.config or {}).get("rubric_version"),
                            stamped_hash,
                            RUBRIC_VERSION,
                            RUBRIC_CONTENT_HASH,
                        )
```

- [ ] **Step 4: Run it**

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest tests/integration/test_run_config_stamp.py -q'`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/main.py tests/integration/test_run_config_stamp.py
git commit -m "feat(runs): stamp SimulationRun.config with the opening rubric version/hash

The run dropdown can now label runs by rubric; resuming under a different
document logs one warning. JSON-column only — no migration. Audit A5.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Documentation — CLAUDE.md, spec cross-links, full gate

No code. Aligns the operator documentation with Tasks 1–7 and records the R0
policy so the next rubric bump doesn't purge the archive again.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/audits/2026-08-28-run-isolation-and-assessment-archive/README.md`
  (status line only)

- [ ] **Step 1: CLAUDE.md edits** (locate each anchor with grep; none of these
  touch the disclosure bullet pinned by
  `tests/unit/test_claude_md_disclosure_sync.py`):

  1. In the `--fresh` paragraph (anchor: `the new **simulation_run_id** IS the
     isolation, every startup and main-loop read is already run-scoped`):
     append after that sentence: `(true since 2026-08-28 —
     ``thread_decisions``/``proposal_reviews`` were the unscoped exceptions
     until then, which fed prior runs' interview summaries into fresh Phase-5
     prompts)`.
  2. Same paragraph, replace `and `profiles/memory/*` is still not reset,
     deliberately and as before.` with: `and `profiles/memory/*` is ARCHIVED,
     not kept: `--fresh` moves it to `profiles/memory/archive/<UTC stamp>/`
     (2026-08-28) so a fresh run's prompts carry no prior-run verdict ledger;
     plain resumes keep memory untouched, and deleting a PI purges their
     archived copies too.`
  3. New box after the `0038` deploy-order box:

```markdown
> ### ⚠️ The assessment archive: never purge, never delete a run row.
>
> `opportunity_assessments` rows are the cross-version comparison corpus —
> each is stamped (`rubric_version`, `rubric_content_hash`) and the read
> paths render it against that revision via `prompts/rubric/revisions.toml`
> + `src/services/rubric_revisions.py`. Three standing rules:
>
> 1. **A rubric regime change is "stamp and keep", never a purge.** The one
>    purge on record (2026-08-27, rubric v3) deleted all 82 pre-3.2.0 rows;
>    they survive only in
>    `backups/opportunity_assessments_pre_purge_1787862739.dump`
>    (restore runbook: docs/plans/2026-08-28-run-isolation-and-assessment-
>    archive-plan.md, Task 9).
> 2. **On every `[meta].version` bump** of `blackbird-rubric.toml`, append
>    the OUTGOING document's entry (version, sha256[:12] of the old bytes,
>    scale, band lines, dimension table) to `prompts/rubric/revisions.toml`
>    in the same commit — otherwise the rows it stamped render as "unknown
>    revision".
> 3. **Never DELETE from `simulation_runs`.** Every run-produced table
>    (`agent_messages`, `opportunity_assessments`, `assessment_drops`,
>    `llm_call_logs`, `specialist_consults`, `thread_decisions`,
>    `pi_dm_messages`, `agent_channels`) is ON DELETE CASCADE from it — one
>    row's delete silently destroys that run's entire archive. No code path
>    does this; the exposure is manual SQL.
```

  4. In the rubric-editing paragraph (anchor: `**Editing the rubric takes
     effect on restart, not on rebuild.**`): append one sentence: `A version
     bump also requires the outgoing document's entry in
     `prompts/rubric/revisions.toml` — see the assessment-archive box.`

- [ ] **Step 2: Append to the audit README** (spec): a line under the TL;DR:
  `**Status 2026-08-28:** remediation plan written and implemented —
  docs/plans/2026-08-28-run-isolation-and-assessment-archive-plan.md (F1–F5
  fixed; A-hazards addressed; restore = Task 9, operator-gated).`

- [ ] **Step 3: Run the FULL gate on the host** (this is the pre-push bar):

Run: `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && ./scripts/ci.sh'`
Expected: alembic sanity, migration round-trip, ruff, full pytest with
coverage floor — all green. (`ruff check` runs with a ratcheted ceiling on
`src/` and zero-findings on `tests/` — fix anything it flags in the new
files.)

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/audits/2026-08-28-run-isolation-and-assessment-archive/README.md
git commit -m "docs: record run-isolation fixes, --fresh memory archiving, and the never-purge archive policy

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Restore the 82 purged rows — OPERATOR-GATED RUNBOOK (no code)

**Do not execute without the operator's explicit go, and only after Tasks 4–6
are DEPLOYED** (restoring first re-creates the misrender the registry exists
to fix). Additive only; deletes nothing. All commands run on the host
(`ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com`), dump verified
2026-08-28: custom-format pg_dump, 82 data rows, 8 distinct
`simulation_run_id`s, all still present in `simulation_runs` (FK-safe), stamp
distribution 1.0.0×4 / 2.0.0×38 / 2.1.0×9 / 2.2.0×4 / 3.0.0×5 / NULL×22.

- [ ] 1. Preflight (no DB writes — it does stage the dump at
  `/tmp/pp.dump` inside the postgres container; if you abort after this step,
  clean that up with `docker exec copi-blackbird-postgres-1 rm -f /tmp/pp.dump`):

```bash
cd /home/ubuntu/blackbird-copi-science/backups
DC="docker compose -f ../docker-compose.prod.yml"
# 82 rows in the dump's data section:
docker cp opportunity_assessments_pre_purge_1787862739.dump copi-blackbird-postgres-1:/tmp/pp.dump
docker exec copi-blackbird-postgres-1 sh -c \
  "pg_restore -a -f - /tmp/pp.dump | grep -c '^[0-9a-f-]\{36\}'"    # expect 82
# every dump run id exists — expect eight '1' lines. The </dev/null on psql is
# load-bearing: `docker compose exec -T` otherwise drains the while-loop's
# stdin and only the FIRST id gets checked.
docker exec copi-blackbird-postgres-1 sh -c \
  "pg_restore -a -f - /tmp/pp.dump | sed -n '/^COPY/,/^\\\\\\./p' | grep -v '^COPY\|^\\\\\\.' | cut -f2 | sort -u" \
  | while read r; do $DC exec -T postgres psql -U copi -d copi -t -A -c \
      "SELECT count(*) FROM simulation_runs WHERE id='$r';" </dev/null; done
# current row count for the postflight delta:
$DC exec -T postgres psql -U copi -d copi -t -A -c \
  "SELECT count(*) FROM opportunity_assessments;"
```

- [ ] 2. Backup current state (same discipline the purge used; each numbered
  block re-establishes `cd`/`DC` so it can be pasted into a fresh shell):

```bash
cd /home/ubuntu/blackbird-copi-science/backups
DC="docker compose -f ../docker-compose.prod.yml"
$DC exec -T postgres pg_dump -U copi -d copi -Fc -t opportunity_assessments \
  > opportunity_assessments_pre_restore_$(date +%s).dump
```

- [ ] 3. Restore (data-only append; UUID PKs cannot collide with post-purge
  rows, but `--exit-on-error` makes any surprise abort loudly):

```bash
docker exec copi-blackbird-postgres-1 \
  pg_restore -a --exit-on-error -t opportunity_assessments \
  -U copi -d copi /tmp/pp.dump
docker exec copi-blackbird-postgres-1 rm /tmp/pp.dump
```

- [ ] 4. Postflight:

```bash
cd /home/ubuntu/blackbird-copi-science/backups
DC="docker compose -f ../docker-compose.prod.yml"
$DC exec -T postgres psql -U copi -d copi -t -A -F' | ' -c \
  "SELECT COALESCE(rubric_version,'(unstamped)'), count(*) FROM opportunity_assessments GROUP BY 1 ORDER BY 1;"
# expect the pre-restore count + 82, with 1.0.0/2.0.0/2.1.0/2.2.0/3.0.0/(unstamped) present
```

  Then spot-check in the UI: one 2.0.0 row's detail page (13 dimensions, 4.0/3.0
  legend, "archived" banner), one unstamped row (live render + disclosure), the
  list page under `run_id=all` (Rubric column populated, off-revision aggregate
  note showing). **Expected and not regressions:** the unvetted-panel banner
  count jumps (restored rows predate `panel_owed`), old runs' detail timelines
  are empty with zero panel cards (their `agent_messages` were destroyed by the
  pre-2026-08-22 `--fresh`; the page says "Interview messages unavailable"),
  and `assessment_drops`/`specialist_consults` for those runs reconnect to
  restored verdicts.

- [ ] 5. Rollback, if ever needed: the restored rows are exactly the dump's 82
  ids (`id` is COPY column 1) — list them with the same COPY-section filter
  the preflight uses, or `cut -f1` alone also emits `SET`/`COPY`/`\.` junk
  lines:

```bash
cd /home/ubuntu/blackbird-copi-science/backups
docker cp opportunity_assessments_pre_purge_1787862739.dump copi-blackbird-postgres-1:/tmp/pp.dump
docker exec copi-blackbird-postgres-1 sh -c \
  "pg_restore -a -f - /tmp/pp.dump | sed -n '/^COPY/,/^\\\\\\./p' | grep -v '^COPY\|^\\\\\\.' | cut -f1; rm -f /tmp/pp.dump"
```

  (Step 3 removed the staged copy, so re-stage it first.) Delete by that id
  list only, never by run or date.

---

## Execution order & deploy note

Tasks 1+2 must land together; otherwise order is free, but 1→8 as written is
simplest. Task 9 waits for a deploy. The eventual deploy (operator-run) is the
standard CLAUDE.md sequence — build `blackbird-app`+`worker`, build `agent`,
`alembic upgrade head` from a one-off container (the branch carries `0038`),
start web tier, and only then a new agent run — plus one new check: the
`--fresh` startup log should show `working memory archived to
profiles/memory/archive/…`.

## Adversarial audit record (2026-08-28)

Two independent context-free auditors attacked this plan against the live tree
and production host after it was written; every finding was applied in place:

- **Engine auditor** (Tasks 1/2/3/7): 2 blockers — the orphan-root guard broke
  the ~22 `_drive_a_consult` tests whose harness drives `_reply_to_thread`
  over an empty log (→ Step 3b + widened Step-4 suite), and Task 2's RED step
  reached a real Anthropic call because `budget_cap=0` is the inert legacy
  cap (→ monkeypatched LLM seam, RED is now positive proof). 3 majors —
  `DeletionReport` is not default-constructible; the memory-fallback claim
  named the wrong symbol; the archive hook sat inside `if not no_db:` so
  `--fresh --no-db` kept the ledger (→ hoisted above the branch). Plus 4
  minors (rationale wording, dead import, interim CLAUDE.md contradiction
  noted, RED expectation completeness). Every Task-1/2/7 anchor, model fact,
  fixture, and the commit-through-shim pattern verified byte-for-byte.
- **Web/registry auditor** (Tasks 4/5/6/9): every registry number re-derived
  from the git blobs and the backup dump — **zero transcription errors**; all
  template/service anchors verified verbatim; the 3.0.0-fixture test traced
  through the new resolve path (passes). 2 blockers — an unused
  `import pytest` (zero-tolerance tests/ ruff gate) and a mid-phrase template
  line break that made the "unknown stamp" assertion unmatchable. 2 majors —
  the runbook's run-id loop only checked one id (`docker compose exec -T`
  drains the loop's stdin; `</dev/null` is load-bearing) and a non-unique
  service anchor. Plus 8 minors applied (provenance wording for 3.0.0/3.1.0,
  stale comments, third "never deleted" site, unique anchors, runbook
  shell-state and rollback-filter fixes).

Residual accepted risks, on record: the tree is concurrently edited by the
verdict-vocabulary workstream (anchors are content-based for exactly this
reason; re-grep before every edit); rows stamped with a hash but no version
fall into the "predates stamping" banner branch (no such row exists in the
corpus or backup); the 3.0.0 registry entry's dimension *titles* and
`pass_label` are carried from 3.2.0 rather than independently attested, and
its own `banding_note` says so.

## Self-review (performed at write time)

- Spec coverage: R0→Task 8/9 policy+runbook; R1→Tasks 1,2; R2→Task 3;
  R3→Task 9; R4→Tasks 4,5,6; R5→Task 7 (+6 label); R6→Task 6; R7→Task 8. F5
  (`closed_thread_ids_subq`) → Task 1(c). No gaps.
- Placeholders: none — every step carries real code/content; the two
  "check while editing" notes (DeletionReport ctor; user-deletion test style)
  name the exact thing to read and the fallback.
- Type consistency: `resolve_revision` tuple shape, `RevisionDimension.weight_note`,
  and the `dimensions` dict keys match between Tasks 4 and 5; view keys match
  between Task 6's service, router, and templates; `_stamp_run_config` is used
  at both creation sites named in Task 7.
