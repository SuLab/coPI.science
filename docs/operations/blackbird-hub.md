# BlackbirdBot (the scout_hub role)

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

BlackbirdBot screens PI ideas against `data/Blackbird_initial_priorities-criteria_v1.pdf`.
**The rubric criteria live in one document, `prompts/rubric/blackbird-rubric.toml`** —
weights, band thresholds, the 1–5 scale, gating criteria, per-dimension evidence lists,
red flags, the heuristic. Since regime 3.0.0 (2026-08-27,
docs/plans/2026-08-27-rubric-v3-consolidation.md) that is SIX single-scale dimensions —
the 13-dimension dual-scale (investment/incubation) machinery, the standalone
target-level checklist, and the stage-selected scoring are all gone, and the operator
directed that no legacy-verdict compatibility be kept: pre-v3 rows are not carried over,
and the read paths render every row against the live document. `src/services/blackbird_rubric.py` loads it once at import (fail-fast on an
invalid document) and renders it into the `{rubric}` placeholder of
`prompts/roles/scout_hub/agent-system.md` at prompt-composition time, so the prompt the
hub reads and the score the code computes cannot drift apart. The `<assessment_json>`
skeleton stays in `prompts/roles/scout_hub/phase4-thread-reply.md` (it is the
authoritative contract for the sidecar's shape);
`tests/unit/test_rubric_prompt_sync.py` is the drift alarm between the two, plus
`specialists.py`'s `maps_to_dimensions`. The per-phase behaviour otherwise lives in
`prompts/roles/scout_hub/` and `src/agent/thread_guidance.py`.

**The review bot is a separate consumer of `prompts/`, not part of BlackbirdBot's
live simulation.** A `review_feedback_analysis` job (`src/services/review_bot.py`)
turns human reviewer feedback with `feedback_mode == "learn"` into a distilled
prompt/rubric-change *suggestion*. Enqueueing is deduped
(`enqueue_analysis_if_absent`) so repeated "learn" feedback on one assessment
still fires at most one pending job, not one per feedback row. The job runs on
the **worker**, which now bind-mounts `./prompts` read-only for exactly this
purpose and reads the prompt files as plain data through the dependency-free
`src/services/interview_transcript.py` loader — it deliberately never imports
`blackbird_rubric.py` (see that module's comment, and the import probe in
`tests/`). The model is `settings.llm_review_model` (`claude-opus-5`), a
setting distinct from the simulation's own model config. A suggestion is never
auto-applied to any prompt file — it is stored as a `PromptChangeSuggestion`
row and surfaced at `/manager/prompt-suggestions` for a human (admin or
manager; a reviewer cannot see this page) to read and act on manually.

**Nothing enqueues a review-bot job automatically as of 2026-09-14.**
`submit_feedback` and `edit_feedback` no longer call
`enqueue_analysis_if_absent`; `POST /reviews/suggestions/generate` — the
"Generate suggestions from current reviews" button on
`/manager/prompt-suggestions`, staff-only and refused under impersonation — is
the ONLY trigger — it lives on the `/reviews` router (whose own explicit POST
allowlist is now eight) and NOT on the manager router, whose write allowlist
stays at the eight routes the Account Types section of `docs/operations/pis-and-access.md` names. `feedback_mode ==
"learn"` now means "eligible for the next
manual generate", not "queued": the page states the eligible count
(`count_pending_analysis_candidates`) and disables the button at zero, and
because the dedupe counts PENDING jobs only, a second press is a visible no-op
(`generated=0`) rather than a duplicate spend. One consequence to know: an edit
made while a job is in flight still leaves its row unconsumed
(`consumed_at_predicates` is untouched and is what guarantees that), but nothing
re-queues it — it waits for the next press.

**One job can now write MORE than one suggestion row.** The reply's contract
gained an optional `additional_proposals` array (at most two entries, each with
its own `target`, `suggestion` and `rationale`), so the same feedback can
propose a hub-prompt change and the matching PI-prompt change together.
`target` stays the JSON object's FIRST key, which is load-bearing:
`_LEADING_TARGET_RE` recovers the declared target from an unparseable reply and
did so for 3 of 12 live replies in the 2026-09-03 evaluation. Each proposal
becomes its own row, all in one commit, all sharing one `feedback_snapshot` and
one `raw_response`. The bot is also now told which prompt SET each file belongs
to (`--- FILE: <path> [<role>] ... ---`) and is given both `role.toml`
manifests; it is told plainly that the per-phase interview guidance is Python in
`src/agent/thread_guidance.py` and has no quotable text. The
bot's LLM calls are, by design, unlogged (no `llm_call_logs` row — that emit
gate needs a callback only the simulation engine installs) and unthrottled (no
rate limiter in front of it): the suggestion row records only `model`,
`transcript_available` and `input_truncated` — no token counts anywhere — so
the Anthropic console is the only cost record; do not go looking for these
calls in `llm_call_logs` or in any per-window rate-limit accounting.

**Review-job lifecycle guarantees (2026-09-02 hardening,
`docs/audits/2026-09-02-review-pipeline/`).** `enqueue_analysis_if_absent`
dedupes against PENDING jobs only — a `processing` job has already
snapshotted its rows and cannot cover feedback written while it waits on the
model. The handler stamps `consumed_at` with a content-conditional UPDATE, so
a row edited or deleted mid-call is left for the job the edit already
enqueued (one WARNING names the count). `_retire_superseded_verdict`
re-points queued job payloads along with the four review tables. The worker
requeues every `processing` row at boot and any older than 30 minutes every
60 s (`requeue_stale_processing_jobs`, `src/worker/main.py`); exhausted ones
go `dead`. The worker's `stop_grace_period: 330s` (working-tree compose edit,
like the others in the two-stack box in `docs/operations/host-and-simulation.md`) exists so a deploy no longer SIGKILLs a
review call at 10 s. A reply that is not valid JSON keeps its declared
`target` via a leading-key regex (`_LEADING_TARGET_RE`,
`src/services/review_bot.py`) rather than defaulting to `out_of_scope`, and
logs one WARNING naming the recovered target — measured at 3 of 12 live
`claude-opus-5` replies in the 2026-09-03 evaluation (reported in the
2026-09-02 audit). The worker's boot sweep
assumes a SINGLE worker instance: `older_than_seconds=0` at boot requeues
every `processing` row regardless of age, with no way to tell a genuinely
abandoned row from one a concurrently running second worker is still
handling — so an ad-hoc second worker would have its live job requeued out
from under it and processed twice.

**Editing the rubric takes effect on restart, not on rebuild.** `prompts/` is
bind-mounted into `blackbird-app` and `agent`, the two services that read it as a
*rendered* document, so a document edit needs no image build there. It is also now
bind-mounted read-only into `worker` (2026-08-28, for the review bot above), but that
mount needs no restart to pick up an edit: the worker reads prompt files fresh, as
plain data, on each `review_feedback_analysis` job rather than importing the rubric
module once at process start. For `blackbird-app` and `agent`, the document is read
ONCE at import, so a running process keeps the rubric it started with. Stop the run,
start it again (see "Before restarting" in `docs/operations/host-and-simulation.md`), and
check the startup banner: it logs `Screening rubric: version X (content hash Y)`. X must
match `[meta].version` in the file; Y is the first 12 hex characters of the file's sha256
(not the full digest). New assessments are stamped with both, so pre-/post-change rows
stay comparable. A version bump also requires the outgoing document's entry in
`prompts/rubric/revisions.toml` — see the assessment-archive box.

> ### ⚠️ The assessment archive: never purge, never delete a run row.
>
> `opportunity_assessments` rows are the cross-version comparison corpus —
> each is stamped (`rubric_version`, `rubric_content_hash`) and the read
> paths render it against that revision via `prompts/rubric/revisions.toml`
> + `src/services/rubric_revisions.py`. Four standing rules:
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
> 4. **Deleting an `opportunity_assessments` row now destroys human work, not
>    just engine output.** `assessment_reviews`, `assessment_review_events`
>    and `assessment_review_assignments` are all ON DELETE CASCADE from
>    `opportunity_assessments.id` (migration `0039`), so a run-row delete under
>    rule 3 — or any other delete of an assessment — takes every reviewer
>    comment, score, approve/disapprove event and assignment on it with it.
>    `prompt_change_suggestions` is the deliberate exception: its
>    `assessment_id` FK is SET NULL, because a suggestion is a distilled
>    artifact of the source assessment, not a record ABOUT it, and is worth
>    keeping even once its source row is gone.

**One interview yields exactly one assessment, and the row you end up with comes
from the LAST verdict-bearing reply.** **A sidecar is now trusted on its own**
(`_sidecar_refusal`, `src/agent/simulation.py:4966`): emitting one IS the hub
saying "this is my verdict", so `_capture_hub_assessment` stores it whether or
not the reply ends the interview. The only refusal left is a re-capture —
`duplicate_thread_verdict`, for a turn already stored, for anything after a
verdict whose reply CLOSED the interview, or for the same ordinal captured twice
— and every refusal is recorded in `assessment_drops`, carrying the model's
`raw_verdict` with it, rather than logged and forgotten. A non-terminal sidecar
is stored as PROVISIONAL and superseded by any later one: last write wins, and
`_retire_superseded_verdict` removes the earlier row (leaving a
`duplicate_thread_verdict` drop as its trace) so the one-row invariant still holds.

`premature_sidecar` is therefore **HISTORICAL ONLY as of 2026-08-22** — no new
rows carry it. It used to mean "a sidecar arrived on a turn that neither
concluded the interview nor closed the thread, so a later turn is still owed the
verdict", and that promise was unbacked: nothing scheduled the later turn,
nothing tracked the debt, and nothing kept the discarded JSON. Two rounds of
evidence killed it. Gating on the ordinal alone destroyed every `pass`: a `pass`
is delivered as a ⏸️ decline, which closes the thread 3-8 ms later in the same
code path, so no ordinal-12 turn ever arrives (run 076e80b6: 4 of 5 refusals were
the thread's terminal message; only 1 of 62 threads reached 12; all 23 `pass`
sidecars ever emitted carried ⏸️). Adding `or closes_thread` rescued the declines
and left the positives exposed, because `phase4-thread-reply.md` binds the two to
MUTUALLY EXCLUSIVE outcomes — Outcome 1 is verdict + sidecar and NO ⏸️, Outcome 2
is ⏸️ and "emit no sidecar" — so the only sidecar the code reliably accepted was
one the prompt forbids. Run 8b64a0e0 measured it: the CONCLUDE door was offered
once in 140 hub reply turns, 0 of 15 sidecars used it, all 13 stored verdicts came
through the ⏸️ door, and the two refused at ordinal 10 included the run's
highest-scoring idea (markham, 3.04, its only `route-to-incubation`) — refused six
minutes before the run's timer ended the interview that was supposedly still owed
a verdict. Gating on *neither* is not the answer either: that wrote three rows for
a single pearce interview (ordinals 8, 10 and 12), because the `<assessment_json>`
contract sits in the STATIC body of `phase4-thread-reply.md` and is therefore in
front of the model on every phase-4 turn — which is what supersession, not
refusal, now handles.

`closes_thread` (the ⏸️ decline, decided by `_reply_closes_thread`, hoisted once
in `_reply_to_thread` and passed down) still matters, just not for admission: with
"is this the CONCLUDE ordinal" it decides whether a verdict is TERMINAL
(`_verdict_is_terminal`), which marks the held record `final` so nothing later can
re-capture it, and which is the only thing that releases the public
`#assessments-summary` headline.

**`_assessed_threads` survives a restart now**, via
`opportunity_assessments.thread_id` (migration `0036`) and
`_rehydrate_assessed_threads`. The map is process-local, so before that a restart
left the engine blind to every verdict it had already written: the interview's own
later turn looked like a FIRST verdict and landed a second row, and a lab bot
⏸️-closing a thread that already held one produced a spurious
`closed_before_verdict` drop. Rows with a NULL `thread_id` — every row written
before `0036` — are skipped rather than guessed at, and the restored record uses
`ordinal=0`, a `final` DERIVED from `_closed_thread_ids`, and an `announced`
READ from `summary_posted_at is not None` (migration `0041`) rather than
defaulted. `ordinal` and `final` are still deliberate choices about which way to
fail if the answer is unknown; `announced` no longer needs one, because the
column now carries the real answer — it used to be hardcoded `False`, which
was the safer of two guesses (a hardcoded `True` would have suppressed the
`#assessments-summary` headline for a verdict stored provisionally before the
restart, and a headline cannot be retracted), but a guess either way traded one
breach for another: reading the column instead means a verdict whose headline
was already public does not get a second one. A pre-`0041` row reads NULL and
therefore `False`, which is exactly the old hardcoded behaviour. All three
fields are documented at the function.

When writing a
test that drives a concluding reply, seed the thread's history in the
`MessageLog`: `_reply_to_thread` overwrites `ThreadState.message_count` from
`get_thread_history`, so `message_count=11` over an empty log is an ordinal-1
EXPLORE turn, not the CONCLUDE turn it looks like.

As of the 2026-08-12 removal cycle (private instructions + reply-only hub), there is no
runtime "private profile" mechanism — `Agent._compose_system_prompt` injects the rendered
rubric but no `## Your Private Instructions` header, and nothing reads
`profiles/private/{agent_id}.md` per-agent. The stale hub copy has now been diffed against
the extracted rubric and archived as
`profiles/private/blackbird.archived-2026-08-20.md` (untracked, git-ignored, unread — no
longer a per-deploy chore) — the diff is recorded in
`docs/audits/2026-08-20-rubric-extraction/blackbird-private-diff.md`. Its one substantive
delta was a fourth **`baltimore_commitment`** gating criterion, deliberately absent from
the tracked rubric: the three gating keys are structural (the sidecar's JSON keys and the
`opportunity_assessments.gating` keys), and `blackbird_rubric.py`'s validator rejects a
fourth outright. (Since 2026-08-24 / rubric v2.1.0 the third key is
`translational_potential`, not `fto_achievable` — FTO was demoted from gate to diligence —
and since v3.0.0 / 2026-08-27 the second key is `credible_science`, not
`credible_tech_source`; pre-rename rows kept their old keys and are not carried over.)

- **Interview guidance is per-role Python**, not a prompt: `src/agent/thread_guidance.py`.
  The `pi_lab` strings there are pinned by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` — do not reword them,
  and never run `pytest --snapshot-update` to make a mismatch go away. (THREE reviewed
  regenerations have occurred, each operator-directed with the diff audited: 2026-08-28
  the PI-bot redline integration (181 hunks, +1144/-614, every changed line machine-traced
  to the four edited files); 2026-08-28 the pi-doc funnel-replacement combine (7 hunks);
  and 2026-08-27, funnel→instrument rewording across the pi_lab
  prompts and `_PI_LAB[EXPLORE]` for rubric v3.x, executed at the operator's direction
  with the `.ambr` diff audited hunk-by-hunk — every changed line belonged to that one
  rewrite. Any future pi_lab change takes the same reviewed-diff path.)
- **Inside an interview thread the hub is reply-only — it never makes a top-level post
  there.** An Opportunity Assessment is not a post type: it is an `<assessment_json>`
  sidecar carried inside the hub's CONCLUDING reply in the interview thread (bare JSON, *no*
  ``` fence). It is stripped from the Slack body before anything is posted and written to
  `opportunity_assessments`, visible at `/admin/assessments`. To the MODEL, `:mag:` names
  the sidecar and is never a post label it may write
  (`prompts/roles/scout_hub/agent-system.md`); it never appears on anything a PI or another
  lab sees. **What is confidential is the sidecar, not the verdict.** The hub's concluding
  reply is *required* to state its verdict inline in the visible `<slack_message>` —
  gating status, recommendation, red flags, confidence label — by
  `src/agent/thread_guidance.py`'s `_SCOUT_HUB[CONCLUDE]` (both strings), by
  `prompts/roles/scout_hub/agent-system.md` and by `phase4-thread-reply.md`: four places,
  all naming those same four things. (The funnel-stage classification was removed in
  rubric v3.1.0 — zero measured entropy at this system's pipeline position; the
  `opportunity_assessments.funnel_stage` column survives, unwritten by new verdicts.) An interview that ended saying nothing would be the
  defect, and when a sidecar is never stored the visible prose is the only surviving record
  of the verdict. What never reaches Slack is the sidecar and what only it carries —
  `raw_verdict`, the computed `weighted_score`, the `band`, and the per-dimension rubric
  scores — measured at **0 leaks across all 1,354 messages** of run 8b64a0e0. The protected
  class in the *visible* half is the PI's own **unpublished** disclosures:
  `phase4-thread-reply.md` binds the visible reply to describe the idea and its evidence
  "only at the level the PI has already made public", confining an unpublished result, an
  unfiled construct, an undisclosed compound or a volunteered limitation to the sidecar — an
  invariant no code and no test currently checks. (Until 2026-08-22 this bullet claimed the
  whole verdict was hidden: `5d67e92` grafted the `#assessments-summary` D12 field list onto
  an unrelated claim about the `:mag:` label, which sent an audit chasing a leak that was
  in fact prompt compliance. See
  `docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md` §1;
  `tests/unit/test_claude_md_disclosure_sync.py` is now the drift alarm.)
  As of the 2026-08-21 manager-PI-controls cycle
  (`SimulationEngine._post_assessment_summary`, `src/agent/simulation.py`), a HELD
  verdict — pass or fail alike — does additionally trigger one genuinely top-level post,
  written by the ENGINE rather than the model and prefixed with that same `:mag:`: a
  headline line (PI/lab name, `company_or_project`, `recommendation`,
  band/score, a permalink or `(link unavailable)`, and — since 2026-09-09 — the
  sidecar's `elevator_pitch` on a second line, clipped at a SENTENCE
  boundary — see below) to `#assessments-summary`
  (`ASSESSMENTS_SUMMARY_CHANNEL`, `src/agent/channels.py`) — deliberately with **no**
  rationale, red flags, gating, or `raw_verdict` (design D12, widened once). The pitch is
  a SIDECAR field and may carry the PI's unpublished disclosures; publishing it rests on
  the operator's assertion (2026-09-09) that PIs cannot join the workspace, which no code
  enforces — `SLACK_INVITE_URL` (`src/routers/agent_page.py:37`) still renders a join link
  on every PI's own `/agent` page. The pitch segment is omitted entirely when
  `elevator_pitch` is NULL, which is every row written before migration `0043`.
  **It is clipped at a SENTENCE boundary, not at an offset (2026-09-14).**
  `PITCH_DISPLAY_CHARS` is still 600 and deliberately was NOT raised — raising it
  would publish more sidecar prose to a channel whose content policy needed
  sign-off — but all 8 pitches on record measured 1173-1406 characters, so the
  old `value[:600]` cut every published headline mid-word (`...picked by univ`,
  `...(as oppose`, `...built on a handfu`). `_clip_at_sentence`
  (`src/services/assessment_headline.py`) now cuts after the last sentence
  terminator leaving at least half the budget, falls back to the last space, marks
  either cut with a `" …"` suffix, and returns a short pitch byte-identically
  unchanged.
  **As of scout_hub 1.9.0 (2026-09-28) the pitch itself is bounded at 250 words**
  (`_PITCH_WORD_LIMIT` in `src/agent/simulation.py`, a WARNING only — an over-long
  pitch still stores), replacing the old four-to-six-sentence, 900-character bound.
  The published excerpt is unchanged: still `PITCH_DISPLAY_CHARS` = 600 characters,
  still clipped at a sentence boundary, so a longer pitch publishes no more prose
  than before — it only makes it likelier that the provenance citation falls past
  the cut, which the engine's citation-loss WARNING reports after the fact.
  **`score_rationale` (sidecar item 10, migration `0048`) is deliberately NOT
  published here** — it reasons about the score, which is exactly the widening
  D12 bounds; it is app-only, on both assessment surfaces. Band/score
  are omitted entirely when the verdict carried no dimension scores, for the same reason
  `_persist_assessment` leaves those columns NULL: an empty `scores` map is "we don't know",
  and `weighted_score({})` is a 0.00 that bands as a decline nobody made. That channel is
  human-joinable/workspace-visible ("public" in the Slack sense — design D11) but is never
  added to `SEEDED_CHANNELS` or any per-agent subscription, so no PI-lab bot is ever joined
  to it or polls it — it still never reaches a PI/lab **agent**'s own view of the
  simulation, only human staff who join the channel directly. The post fires synchronously
  right after `_persist_assessment` returns HELD inside `_capture_hub_assessment` — but
  only for a verdict that is **TERMINAL and not already announced** for that interview
  (`announce = terminal and not already_announced`, `simulation.py:3605`). That
  condition is not the same as "held", and the difference arrived with provisional
  storage: since a non-terminal sidecar is now STORED rather than refused, one interview
  can hold several verdicts in turn, and a headline is a public Slack post that cannot be
  retracted when the row it described is superseded moments later. So a provisional verdict
  is stored, visible to staff, and logged as `Provisional verdict stored ... no
  #assessments-summary headline until the interview concludes` — announced only when a
  terminal reply arrives. `announced` carries forward across supersession for the same
  reason. A dropped
  or refused sidecar (an `AssessmentDrop` row, never an `opportunity_assessments` row) never
  posts (design D14), and a Slack failure in the post/permalink step is caught and logged,
  never raised into the calling turn (design D16) — see
  `docs/specs/2026-08-21-manager-pi-controls-design.md`. With `SLACK_ENABLED=false` the
  headline is skipped outright (the hub's transport is a `NullTransport`, which has no
  async post/permalink methods at all) — the assessment row is still written, so nothing is
  lost but the Slack copy.

> As of 2026-08-29 that is no longer the ONLY path. A verdict whose interview
> ends without a terminal reply — the `max_thread_messages` timeout, an
> abandoned thread, or the run's own shutdown — is announced by
> `_announce_owed_headline`, queued by `_close_thread` and drained by
> `_drain_and_flush` / `stop()`. Announcement is now a property of the
> INTERVIEW ENDING, not of one particular reply, and `at-most-once` is enforced
> by the `opportunity_assessments.summary_posted_at` column rather than by
> in-memory state alone. Each rescue logs one **WARNING** naming the trigger —
> a run with several means the hub is being locked out of its own CONCLUDE
> ordinal (RCA §2.2), which this path makes non-destructive but does not fix.
> See `docs/audits/2026-08-29-lost-assessment-headlines/README.md`.

> Since 2026-09-29 (audit remediation P0-08) every headline is CLAIMED before it posts:
> `src/services/headline_claims.py` sets `summary_claimed_at` on every owed row of the
> interview and refuses when any row of it is already claimed or posted. The engine (at
> capture, at a close, in the stop sweep) and `scripts/backfill_assessment_headlines.py`
> both go through it. A definite failure releases the claim; a transport error with no
> Slack response keeps it (IN DOUBT) and nothing re-posts it. A verdict whose first write
> was queued is never claimed at capture; its headline waits for the close or the sweep,
> and a supersession carries the retired row's stamp and claim onto the replacement in the
> same transaction. Which interviews a stop sweep announces depends on the end reason
> (`docs/operations/host-and-simulation.md`, "How a run ends").

- **`weighted_score` is computed**, never taken from the model:
  `src/services/blackbird_rubric.py`. `recommendation` (which may be
  `route-to-incubation`) comes straight from the model's verdict and the computed `band`
  comes straight from `weighted_score` — they are separate columns on
  `opportunity_assessments` and neither is derived from the other.
  A fourth write-time fact joined them in `0036`: **`panel_owed`**, the specialist
  floor's own answer to "was a panel owed here", computed once by `panel_is_owed` in
  `_persist_assessment` (`simulation.py:4505`) and **replayed** by the read path rather
  than recomputed. That is the point of the column. `assessment_detail.panel_state`
  used to ask `panel_is_owed(recommendation, band)` at RENDER time, which answers a
  different question — "would a panel be owed under TODAY's rules" — so every widening
  of the predicate silently re-labelled every older row. It widened twice in 2026-08
  alone, and 12 production rows written by the recommendation-only floor were re-read by
  the band-aware page as completed audits; at least five had a demonstrable gap.
  `panel_state` now returns **five** states — `gap`, `unverified`, `unrecorded`,
  `not_owed`, `verified` — and reaches `verified` (the green box) ONLY via
  `panel_owed is True`. `unrecorded` is `panel_owed IS NULL`: the row predates `0036`,
  or was backfilled, or was hand-built by a test, and no claim is available for it.
  `tests/unit/test_panel_state.py::test_the_read_path_never_re_derives_the_floor_s_decision`
  fails if anyone puts `panel_is_owed` back in front of the column test.
- **The specialist vocabulary is `blocking` / `gap` / `adequate`** (renamed from
  `blocking` / `caution` / `clear` on 2026-08-28; only `blocking` survived). The write set
  and the read set are deliberately DIFFERENT sizes, and that asymmetry is load-bearing:
  `VERDICT_SIGNALS` (the three live labels) is what a persona may offer and what the
  calibration ladder admits, while `_READABLE_SIGNALS = VERDICT_SIGNALS |
  HISTORICAL_VERDICT_SIGNALS` is what `parse_opinion` will READ. Two paths re-parse
  *stored* reply text — `/admin/activity/{run}/llm-calls` and the retro assessment-detail
  parse — so reading against the live three alone would re-render all ~1,192 pre-rename
  consults as a defaulted `gap` and log a WARNING per row per page view. This is not
  "legacy-verdict compatibility" in the sense the rubric section rules out: nothing writes
  a retired label, no persona offers one, and no verdict is carried over — only historical
  text stays readable. The admin templates keep colour/glyph branches for the retired pair
  for the same reason, and `adequate` takes ☑ where `clear` took ✓ so the two are
  distinguishable on sight.
- **Panel notes and consult truncation:** Panel notes clip the hub's question
  at `PANEL_NOTE_QUESTION_CHARS` (src/agent/specialists.py, recalibrated 850
  on 2026-08-26), and `clip_rate_warning` logs once per run when >10% of >=20
  posted notes clip — that WARNING means the calibration has decayed again;
  remeasure from `specialist_consults` question lengths. Truncated-consult
  cause (refusal vs max_tokens ceiling) is derivable per call from
  `llm_call_logs.call_stats[].stop_reason` — see
  docs/audits/2026-08-26-specialist-truncation-rca/README.md.
- **`gating` values are the tri-state strings** `"met"` / `"not_met"` / `"unconfirmed"`,
  never booleans — "the PI declined" and "we never asked" are different answers, and only
  the former can license discounting an idea.
- **`AssessmentDrop.reason` gained `unwritable_row`** on 2026-08-22, and it is the one
  reason that is not a GATE decision: the engine WANTED the row and the database refused
  it, even alone, during `_recover_rows_individually`'s per-row retry after its batch
  failed (`_flush_persisted`). The verdict was already concluded, parsed and assembled
  into a row before it was lost, so the drop is its only surviving trace — `raw_verdict`
  carries the verdict exactly as the row would have stored it and `detail` names the
  database's own exception plus the channel/thread. Not retried (it already failed twice,
  batch then alone), and recording it is itself best-effort, since a malformed row must
  not take its batch's surviving verdicts down with it. `premature_sidecar` and
  `specialist_floor` are the two HISTORICAL-ONLY reasons; the full list, with what each
  one costs, is on the model (`src/models/opportunity.py`).
- **`search_prior_art` is a TITLE-only search** on the USPTO Open Data Portal (PatentsView
  was decommissioned in its 2026-03-20 migration to api.uspto.gov). It backs off to the
  most specific terms when the full phrase misses — before that backoff existed, every
  production search ANDed in domain-generic words like "inhibitor" and returned zero hits,
  reported to PIs as clean novelty. An empty title search is never FTO.
  **The query the model asks for is not always the query that is sent**, and as of
  2026-08-22 every difference is disclosed to it. `_prepare` (`src/services/patents.py:169`)
  NFKD-normalises and transliterates each whitespace chunk before tokenising: Greek is
  spelled out (`Qβ` → `Qbeta`), Unicode dashes and combining marks are folded away, and a
  chunk with no ASCII equivalent at all is DROPPED. `AND`/`OR`/`NOT` are dropped too —
  they are query syntax, not title words, and the terms are ANDed for the caller anyway;
  the tool description the model sees now says so outright. Each of those lands in
  `PriorArtResult.dropped_or_rewritten` and is rendered into the tool result by
  `tools.py::_rewrite_note`, alongside `.broadened` (the backoff fired, so hits may be
  adjacent) and `.truncation_note` (`.hits` is one page, not the whole match set). The
  disclosure has to be TOTAL: the first version fired only when the fold CHANGED a chunk,
  so `π-π stacking` reached the model as "SCOPE: searched titles for stacking." with
  `broadened` False — a term silently deleted and the note saying nothing had happened,
  which is the same class of damage the transliteration exists to prevent.
