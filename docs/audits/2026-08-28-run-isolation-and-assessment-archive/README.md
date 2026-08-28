# Adversarial audit: assessment storage, bot run-isolation, and the assessment archive

**Date:** 2026-08-28. **Scope:** (1) whether a `--fresh` run's bots fully disregard
every previous run's posts; (2) whether the run dropdown supports viewing older
assessments so humans can compare system versions. Production DB read directly
(read-only) on 2026-08-28; prod schema stamped `0037`; all engine line numbers
against the working tree at commit `b648be5`.

## TL;DR

Neither goal holds today, for different reasons.

- **Isolation:** the DB/Slack plumbing is mostly right, but three unscoped startup
  queries feed previous runs' interview outcomes into live prompts, and
  `profiles/memory/*` — deliberately preserved across `--fresh` — is a cross-run
  verdict ledger injected into **every** system prompt. A fresh run's hub starts
  "remembering" ~40 labs' verdicts from runs it never saw.
- **Archive:** the run dropdown exists and filters correctly, but the corpus it
  would show was deleted: the 2026-08-27 rubric-v3 residuals purge removed all 82
  pre-3.2.0 assessment rows (backed up, verified restorable). The table holds 6
  rows, all v3.2.0, all run `61ccad6d`. And the read paths render every row
  against the LIVE rubric document, so a restored old-version row displays six
  blank dimensions, live weights, and a live band legend that contradicts its
  stored band. `simulation_runs` records no rubric/code version at all.

The structural conflict: the current policy treats a rubric regime change as
license to purge, while the operator's goal treats old-regime rows as the whole
point. One of those has to yield, and everything in §4 follows from choosing
"never purge; render version-aware".

---

## 1. How assessments are stored and referenced (mechanics, verified)

**Storage.** One interview → one `opportunity_assessments` row (last
verdict-bearing reply wins; supersession via `_retire_superseded_verdict`,
`src/agent/simulation.py:4015-4024`, scoped to the current run by
`_superseded_row_filter` at `:4081-4089`). Rows carry `simulation_run_id`
(NOT NULL, FK → `simulation_runs` **ON DELETE CASCADE**), `rubric_version` /
`rubric_content_hash` stamps (since `0030`), `panel_owed`, `thread_id` (since
`0036`), and the computed `weighted_score`/`band` alongside the model's
`recommendation`. Refusals land in `assessment_drops`. Specialist opinions land
in `specialist_consults` (stamps arrive with `0038`, not yet deployed).

**What bots reference.** Agents never query `opportunity_assessments`,
`assessment_drops`, or `llm_call_logs`; they have no Slack-history or DB-recall
tool (`src/agent/tools.py` allow-lists per `prompts/roles/*/role.toml`). Their
view of the world is: the in-memory `MessageLog` (hydrated from run-scoped
`agent_messages` reads — all five `select(AgentMessage)` sites in `src/agent/`
filter on `simulation_run_id`), the per-channel in-RAM poll cursors, the
`profiles/public/*.md` personas, `profiles/memory/*` working memory, and the
startup state rebuilt by `_rebuild_agent_state`. The last two are where
isolation fails (§2).

**Empirical check (prod, 2026-08-28):** joining `agent_messages` on
`(slack_thread_ts = parent.slack_ts, same channel)` finds **1,120 same-run
parent/child pairs and 0 cross-run pairs** across the five fresh boundaries
since 2026-08-22 — no bot has ever replied into a previous run's Slack thread.
Slack-side isolation genuinely works; the leaks are DB- and disk-side.

---

## 2. Findings — fresh-run isolation ("bots must fully disregard previous posts")

Ranked by impact. F1 is a product decision; F2–F4 are unambiguous bugs
(the run column exists and is written on every row; the reads just don't use it).
CLAUDE.md's claim that "every startup and main-loop read is already run-scoped"
is **false as stated** because of F2–F4.

### F1 — `profiles/memory/{agent_id}/public.md` survives `--fresh` and is injected into every prompt
- Writer: `Agent.update_working_memory_file` (`src/agent/agent.py:563-586`), from
  the thread-close synthesis (`simulation.py:7727-7750`), which explicitly asks
  for "ideas pitched and their screening status" and seeds each synthesis with
  the previous one — so content compounds across runs indefinitely.
- Reader: `_compose_system_prompt` → `## Your Working Memory`
  (`agent.py:336-337`), unconditionally, in both the Phase-4 and Phase-5 system
  prompt builders.
- Verified content (2026-08-28): `profiles/memory/blackbird/public.md` (mtime
  2026-08-27) is a per-lab verdict ledger — "Wu … Culotta … cai — All closed
  **no_proposal**", "Slusher: closed pass — asset stage, not science", named
  patent blockers (Van Zijl US20250339051A1), "Shastri PI deceased". Lab-side
  files carry "Do not re-pitch unless a positive control is established."
- Consequence: a `--fresh` run's hub is primed to treat first-time pitches as
  re-pitches; lab bots self-censor ideas the new run has never heard. This
  **transitively contaminates specialist consults** too: the hub writes the
  consult `context` string (`tools.py:608-616`) from a turn whose prompt
  contains the ledger.
- Status: persistence is deliberate and documented (`src/agent/main.py:124-125`,
  CLAUDE.md). It is nonetheless incompatible with the stated requirement; §4 R2.

### F2 — `_rebuild_agent_state` loads `thread_decisions` UNFILTERED → previous runs' interview summaries enter Phase-5 prompts verbatim
- `sa_select(ThreadDecision)` with no `.where()` at `simulation.py:6696`
  (verified in-file). `ThreadDecision.simulation_run_id` is NOT NULL and written
  correctly by `_close_thread` (`:2489`); the read ignores it. The guard at
  `:6692` also checks only `session_factory`, not `simulation_run_id` — unlike
  the correctly-gated `llm_call_logs` reads at `:6896`/`:6926` in the same
  method.
- Flow: rows → `self._prior_threads` (`:6713`, `summary_text[:400]` verbatim) →
  `_get_prior_threads_for_agent` (`:2789`) → `build_phase5_prompt`
  (`:2930`) → the `{prior_conversations}` block (`agent.py:508-530`), under
  `prompts/phase5-new-post.md`'s instruction "Do NOT re-pitch an idea the hub
  has already screened."
- Consequence: the very first Phase-5 turn of a fresh run shows each lab bot up
  to five of its previous runs' closed interviews with outcomes and summaries.
  Same rows also pollute `self._closed_thread_ids` (`:6699`, `:6723`) — see F4.

### F3 — pending proposals / reviews / finalized-channel state cross runs
- `sa_select(ThreadDecision).where(outcome == "proposal")` — no run filter
  (`simulation.py:6806-6810`, verified). `sa_select(ProposalReview...)` — no
  filter (`:6813-6818`; the table has no run column, so it must be scoped via
  its `thread_decision_id` join). `_finalized_private_channels` derived from the
  same unfiltered list (`:6845-6847`).
- Consequence: a fresh run's agent can start **benched** by a previous run's
  unreviewed proposal (an unreviewed entry blocks its agent, `:7005` area), and
  same-named private channels are suppressed from re-opening.

### F4 — the cross-run `_closed_thread_ids` pollution currently MASKS a re-open hazard; the F2 fix must not ship alone
- Old runs' thread ids in `_closed_thread_ids` are what blocks Phase-3 from
  re-activating an old thread (`:1771`, `:1815`, `:1862`).
- If a reply in a previous run's thread ever IS ingested, the run-scoped
  `MessageLog` has no root for it: `get_thread_history` returns just the new
  reply → `message_count = 1` → ordinal 2, EXPLORE — a concluded 12-message
  interview would restart from scratch, and `get_thread_allowed_agents` returns
  `None` for a missing root (`message_log.py:577-579`), so the participation
  check waves it through.
- Today's ingest surface for such a reply is a single edge: the poller reads
  `conversations.history` only (top-level + `thread_broadcast`), drops all human
  messages outright (`simulation.py:5285-5288`, verified), and the only
  `conversations.replies` caller is the resume-only reconcile (`:6645`). Our own
  agents never set `reply_broadcast`. So the hole opens only for another
  workspace bot's broadcast reply — rare, but the protection is an accident of
  two bugs cancelling.
- Fix shape: run-scope `:6696` **and** add an orphan-root guard in
  `_reply_to_thread` (evict a thread whose root is absent from the log) in the
  same change.

### F5 — minor: `closed_thread_ids_subq` unfiltered
- `sa_select(ThreadDecision.thread_id)` at `simulation.py:5868` widens the
  NOT-IN set used by `_rebuild_state_from_db`. Near-zero practical impact
  (thread ids are timestamps); fix for consistency.

### What already holds (verified, give it credit)
- Poll cursors are in-RAM per channel, never persisted; the fresh branch of
  `_restore_slack_state` (`:6430`) parks every channel's cursor at its newest
  message via `_seed_slack_cursors_without_ingest` (`:6435-6545`) with a
  wall-clock fallback — no "0"-cursor re-import.
- All five `select(AgentMessage)` sites, `_rehydrate_assessed_threads`
  (`:4213`), `_seed_consults_from_db` (`:4621`), `_sync_private_channels_from_db`
  (`:2632`), the pi-inbox cursor and poll (`:5955`, `:5383`), and both
  `llm_call_logs` limiter rebuilds are run-scoped.
- No agent tool or prompt path reads Slack channel history; `#assessments-summary`
  is outside the poller's scope; `pi_dm_messages` is a dead table (no reader).
- `profiles/public/*.md` is persona-only (sole writer
  `src/services/profile_export.py:117`); no run content accumulates there.

---

## 3. Findings — the archive and the run dropdown ("compare which versions worked")

### A1 — the archive was already deleted (REALIZED loss, recoverable)
The 2026-08-27 rubric-v3 residuals purge (operator-directed, R2 of
`docs/plans/2026-08-27-rubric-v3-consolidation.md:322-327`) deleted all 82
pre-3.2.0 rows in one transaction. Verified against prod on 2026-08-28:
`opportunity_assessments` holds **6 rows, all `rubric_version = 3.2.0`, all run
`61ccad6d`**, out of 15 runs on record. The backup
`backups/opportunity_assessments_pre_purge_1787862739.dump` (custom-format
pg_dump, host: `/home/ubuntu/blackbird-copi-science/backups/`) was verified
today to contain **exactly 82 data rows** plus table DDL, PK, indexes, and the
FK to `simulation_runs`. `specialist_consults` (9 runs) and `assessment_drops`
(8 runs) were kept and now reference verdicts that no longer exist.

So: the run dropdown works, and shows an empty table for every historical run.
If the purge-on-regime-change policy continues, the comparison corpus can never
accumulate — this is the central conflict to resolve (§4 R0).

### A2 — the dropdown itself is fine; provenance display is not
`list_assessments` (`src/services/directory.py:268-287`, verified): options =
every `SimulationRun` newest-first, default = newest run, `?run_id=all` escape
hatch, filter on `simulation_run_id`, and all aggregates share the scope. But:
- No rubric-version column, filter, or sort on either list page; the only
  version string on the page is the **live** one, whatever run is selected.
- Per-row run labels render only under `run_id=all`; the detail body header
  names no run; the manager detail page has no run reference at all.
- A purged/empty old run renders "No assessments recorded yet." — byte-identical
  to a brand-new run.

### A3 — read paths render every row against the LIVE rubric document
`assessment_detail.py:525-550` (verified): iterates live `RUBRIC_WEIGHTS`,
`.get()`s the row's scores by live key, uses live `scale_max`. The row's own
stamps are display-only. For any row whose key space differs from the live
document (every pre-v3 row; every v3 row once v4 lands):
- All stored dimension scores vanish — six "not scored" bars with live weights
  and a tooltip ("an unscored dimension drags it down") that misdescribes the
  row. The scores survive only in the admin-only raw-verdict JSON.
- The band legend prints live thresholds (3.4/2.8) beside a stored band computed
  under the old ones (4.0/3.0) — a stored `3.2 / pass` reads as arithmetically
  wrong.
- List-page `dimension_stats` are live-keyed (`directory.py:427-433`), so
  off-version rows contribute n=0 silently; `band_counts` pools threshold
  regimes under `run_id=all` with no warning.
- The one version-aware widget is the per-row detail banner
  (`_assessment_detail_body.html:157-173`): "Scored against rubric X (hash)"
  with an amber not-comparable warning on mismatch. That is the seed to build on.
- Stored `weighted_score`/`band`/`recommendation` are never recomputed at read
  time (write-path only) — good; a restored archive keeps its numbers.

### A4 — the UI's retention promise is now false
`templates/admin/assessments.html:67-69` (and the manager twin): "assessments
from earlier (e.g. `--fresh`-wiped) runs still exist and are never deleted" —
verified present, and false since 2026-08-27.

### A5 — runs carry no version identity
`SimulationRun` columns: id, started_at, ended_at, status, total_messages,
total_api_calls, config. `config` (written at `src/agent/main.py:254-261`)
holds six scheduler knobs — no rubric version/hash, no git SHA, no image tag.
The startup banner computes "Screening rubric: version X (content hash Y)" and
logs it to stdout only. With the stamped rows purged, **nothing in the database
can say which rubric a given run used.**

### A6 — one-statement total loss remains possible
Every run-produced table (`opportunity_assessments`, `assessment_drops`,
`agent_messages`, `llm_call_logs`, `specialist_consults`, `thread_decisions`,
`pi_dm_messages`, `agent_channels`) is `ON DELETE CASCADE` from
`simulation_runs`. No code path deletes a run — the exposure is manual SQL, and
nothing guards it.

### A7 — old runs' interview timelines are already gone
Prod has `agent_messages` only for the six runs since 2026-08-22 (the old
destructive `--fresh` deleted the rest). The detail page reconstructs the
timeline from run-scoped `agent_messages` anchored on `slack_ts`
(`assessment_detail.py:653-707`; the stored `thread_id` column is never read),
so restored pre-2026-08-22 rows will show an empty timeline and zero panel
cards — which reads as "no panel was convened", not "transcript destroyed".

### A8 — smaller archival rots
- `user_deletion` correctly retains assessments but nulls `AgentRegistry.user_id`,
  silently degrading archived rows' lab links to plain text.
- `assessment_drops`/`specialist_consults` orphaned by the purge (see A1).

---

## 4. What follows (recommendations, ordered — none implemented in this audit)

- **R0 (policy, the operator's call):** pick "never purge; render version-aware"
  over "purge on regime change". Every other recommendation assumes it.
- **R1 (small bug fix):** run-scope `simulation.py:6696` and `:6806`, scope
  `ProposalReview` via its `thread_decision_id` join, tighten both guards to
  require `simulation_run_id`, scope `:5868` — **paired with** the orphan-root
  eviction guard in `_reply_to_thread` (F4). Then correct CLAUDE.md's
  "every read is run-scoped" line.
- **R2 (product change, matches the stated requirement):** make `--fresh`
  archive-and-reset `profiles/memory/*` (e.g. move to
  `profiles/memory/archive/<old_run_id>/`) so a fresh run starts blind. Plain
  restarts of the same run must keep memory — only `--fresh` resets. This also
  closes the transitive consult-context leak.
- **R3 (restore):** reinsert the 82 backed-up rows into `opportunity_assessments`
  (they carry their original `simulation_run_id` and stamps; the dropdown picks
  them up unchanged). Do this after R4/R5, or the pages will misrender them per A3.
- **R4 (version-aware reads):** persist each rubric document by content hash
  (a `rubric_revisions` table written at startup, or a loader over git history)
  and render each row's dimensions/legend against its **stamped** revision,
  falling back to the current banner warning when the revision is unknown. Add a
  rubric-version column + filter to both list pages; split or label aggregates
  by version instead of silently pooling.
- **R5 (run identity):** stamp `rubric_version`, `rubric_content_hash`, and the
  git SHA into `SimulationRun.config` at startup (the banner already computes
  the first two), and render them in the dropdown label — the dropdown then IS
  the system-version selector the archival goal wants.
- **R6 (honesty):** fix the A4 template claim; distinguish "run predates the
  archive / was purged" from "no assessments yet"; show run + stamp in the
  detail header (manager page included).
- **R7 (guardrails):** a CLAUDE.md box on the `simulation_runs` CASCADE, and
  treat any future regime change as "stamp and keep", with the purge path
  requiring an explicit backup-verified runbook (as R2-2026-08-27 in fact did).
