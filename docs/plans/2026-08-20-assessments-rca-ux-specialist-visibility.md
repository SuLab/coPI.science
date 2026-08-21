# Assessments: "all pass" RCA, page UX, and specialist visibility

**Date:** 2026-08-20
**Status:** **IMPLEMENTED** 2026-08-20 — commits `960473c..d6b8a67` (10) on branch
`blackbird`; full `./scripts/ci.sh` gate green (2212 passed, coverage 77.19%). Everything
in §2, §3 and §4 is built except calibration (§1.4 R2/R3, deliberately deferred by
decision). **NOT DEPLOYED**: migration 0030 is unapplied in production and the agent/web
images are not rebuilt — see CLAUDE.md's new 0030 deploy blockquote for the mandatory
migrate-before-serve order. Decision recorded 2026-08-20: **calibration itself is
deferred**; the rubric now lives in `prompts/rubric/blackbird-rubric.toml` (§4) — the
later calibration is an edit to that document, not a code change.
**Scope:** four workstreams — (1) make `/admin/assessments` readable, (2) root-cause why
every assessment is "pass", (3) make specialist responses and tool calls in interview
threads explicit and visible, (4) extract the rubric into one reviewable document.

Everything below was verified directly against the production database
(`copi-blackbird-postgres-1`, db `copi`) and the deployed repo on 2026-08-20. No claim in
§1 is inferred from documentation alone; each has a stated evidence source.

---

## 1. Root cause analysis: why every assessment is "pass"

### 1.1 The observable facts (all verified in prod)

| Fact | Evidence |
|---|---|
| 29/29 stored assessments have `band='pass'`; weighted scores span **1.85–2.89**, all below the 3.0 "conditional" line | `SELECT recommendation, band, count(*), min/max(weighted_score) FROM opportunity_assessments` |
| Recommendations: **16 `pass`, 13 `route-to-incubation`, 0 `advance`, 0 `conditional`** | same query |
| The arithmetic is exactly correct: recomputing all 29 scores with `src/services/blackbird_rubric.py` reproduces every stored value to the digit (0 mismatches) | recomputation script run 2026-08-20 against exported `scores` JSONB |
| The scores are genuine, well-formed model output: all 13 rubric keys present and numeric in all 29 rows; no key-name mismatches, no nesting artifacts, no NaN | same export; per-dimension n=29 for every dimension |
| `raw_verdict->>'recommendation'` equals the stored `recommendation` in all 29 rows — nothing is transformed or clipped between model and page | `SELECT raw_verdict->>'recommendation', recommendation, count(*) ...` |
| All 29 are `funnel_stage='incubation'` — the population is entirely early academic pitches | `SELECT funnel_stage, count(*) ...` |
| The model **did** emit exactly two `conditional` verdicts (PI `gordy`, 2026-08-17 23:47 and 23:55) — both were **refused and destroyed** by the then-deployed specialist-floor check ("required the chemistry specialist, never consulted") | `assessment_drops` rows, reason `specialist_floor` |
| That refuse-and-drop behavior was fixed to flag-and-store in `1a32e43` (2026-08-18), deployed with the 2026-08-19 15:48 image — i.e. **after** almost all of the current data was collected | git log + `docker images` timestamps + drop timestamps |
| Specialist panel signals across all 239 consults ever run: **caution 191, blocking 47, clear 1** | `llm_call_logs` phase `consult_%`, `response_text` signal counts |
| A hypothetical verdict built from the *best score ever observed on each dimension* still only reaches **3.6 ("conditional")** — the "advance" band (≥4.0) has never been reachable with real scores | recomputation script |

Per-dimension distribution over the 29 verdicts (weight, mean, max):

```
differentiation              w=15  mean=3.52  max=5   ← the one strong dimension
market_unmet_need            w=12  mean=2.62  max=4
mechanism_validation         w=12  mean=2.34  max=4
team                         w=10  mean=3.31  max=4
toxicity_selectivity         w=10  mean=2.03  max=3
experimental_rigor           w=10  mean=2.59  max=4
external_signals             w= 8  mean=1.17  max=2   ← pinned at floor
chemistry_dc_path            w= 8  mean=1.52  max=3
ip_fto                       w= 6  mean=1.41  max=2   ← pinned at floor
platform                     w= 4  mean=2.41  max=3
dev_regulatory_feasibility   w= 3  mean=1.86  max=3
workplan_capital_efficiency  w= 1  mean=2.45  max=4
exit_thesis                  w= 1  mean=1.52  max=2   ← pinned at floor
```

### 1.2 Root cause: not a bug — a calibration property plus a presentation problem

**There is no computation defect.** Five verified layers compound to produce the
observed result:

1. **The population can't score on ~30% of the rubric.** Every assessment is an
   incubation-stage academic pitch. The scale anchors in
   `prompts/roles/scout_hub/agent-system.md` §"Weighted scoring dimensions" are
   investment-grade: `external_signals` tops out at "≥2 VCs/funders interested",
   `ip_fto` at "FTO secured", `exit_thesis` at "credible staged exits with comps and
   valuation ranges". An unformed academic idea scores 1–2 on these *by definition* —
   and the measured means (1.17, 1.41, 1.52) confirm the model applies them honestly.
   `external_signals + ip_fto + chemistry_dc_path + exit_thesis + dev_regulatory` =
   26 weight points that are structurally pinned near the floor for this population.

2. **The band thresholds were never re-baselined after the weights were re-cut.**
   `band()`'s 3.0/4.0 lines come from Part C.3 of the priorities PDF (written for
   deal screening); the weights were later re-cut from 100% commercial to 60/40
   commercial/scientific (`RUBRIC_WEIGHTS` docstring) — explicitly to catch
   *rejections* — but the absolute thresholds were carried over unchanged. Fixed
   investment-grade thresholds applied to incubation-stage pitches yield a ceiling of
   ~2.9 observed (3.6 even for a best-of-everything composite): **the "advance" band
   is empirically unreachable, and "conditional" nearly so.**

3. **Every prompt layer is calibrated skeptical, and skepticism composes.** The
   CONCLUDE guidance says "Option 2 [⏸️ decline, no assessment] is perfectly
   acceptable — **most interviews should end there**" (`thread_guidance.py:148`);
   DECIDE says "an honest 'no' is more useful to Blackbird than an inflated maybe";
   the system prompt says "Do not manufacture an assessment just to have one"; the
   specialist personas are told "Do not recommend advancing or passing", and their
   output signals ran caution/blocking/clear = 191/47/1. Each choice is individually
   defensible; multiplied together they leave nothing above 3.0.

4. **Verdict costs are asymmetric, and the floor destroyed the exceptions.**
   `advance`/`conditional` require mandatory specialist consults and carry a refusal
   threat ("refused and nothing is persisted" — `phase4-thread-reply.md:63`,
   restated at CONCLUDE); `pass`/`route-to-incubation` "require no panel at all."
   The prompt itself warns the model not to let this push it toward weaker verdicts —
   an acknowledgment that the pull exists. And the only two `conditional` verdicts
   ever emitted were in fact refused and destroyed by the pre-`1a32e43` floor code.
   (Fixed since 2026-08-19: such verdicts now persist flagged `panel_incomplete`.)

5. **"pass" doesn't mean what a reader thinks it means, and the positive verdicts are
   invisible.** Per the PDF vocabulary, band "pass" = *pass on the deal* (decline) —
   `blackbird_rubric.py:162` documents this. Meanwhile the rubric's own banding text
   defines route-to-incubation as a *sub-case of the <3.0 band*: "<3.0 → pass (**or
   route to a grant/incubation de-risking step if differentiation is high but data is
   thin**)". The model follows this faithfully — its 13 route-to-incubation verdicts
   average differentiation 3.8 vs 3.3 for pass. **For an incubation-stage population,
   route-to-incubation IS the positive outcome — and 13 of 29 assessments (45%) got
   it.** But the page's headline cards count by band (Advance 0 / Conditional 0 /
   Pass 29), and every row renders a gray uppercase "PASS" next to the score. The
   screening funnel is arguably working; the page presents it as everything failing.

### 1.3 Hypotheses tested and rejected

- *Malformed/mismatched score keys dragging the mean down* — rejected: 13/13 keys
  present and numeric in 29/29 rows; prompt skeleton keys byte-match `RUBRIC_WEIGHTS`.
- *Arithmetic/NaN/rounding defect in `weighted_score()`/`band()`* — rejected: 0/29
  mismatches on recomputation with the production code.
- *Recommendation overwritten or derived from band* — rejected: separate columns,
  `raw_verdict` matches stored values in all rows.
- *Sidecar parsing corrupting scores* — rejected: same evidence.
- *Floor currently discarding positive verdicts* — true only historically (2 verdicts,
  pre-2026-08-19); current code flags instead of dropping. The two lost `conditional`
  verdicts are unrecoverable (concluding replies posted, no later turn to re-emit).

### 1.4 What "fixing" this means — a decision, not a patch

Three distinct, non-exclusive remedies, in increasing order of invasiveness:

- **(R1) Present the verdict the system actually produces.** Make `recommendation`
  the primary column/counts and label band "pass" as "pass (decline)". Zero model
  change; 45% of existing rows immediately read as positive routing. *Recommended
  regardless of anything else.* (Folded into §2.)
- **(R2) Stage-aware banding.** Either per-stage thresholds, or a stage-relative
  presentation (e.g. percentile within funnel stage), computed in
  `blackbird_rubric.py` as `band()` is today — never model-supplied. Requires a
  policy decision about what "conditional" should mean for an incubation-stage idea.
- **(R3) Re-anchor the scale for the incubation stage.** Add per-score anchor
  examples to the rubric prompt ("what a 3 on external_signals looks like for an
  unformed academic idea"), and/or revisit the "most interviews should end there"
  prior and the consult-cost asymmetry (e.g. require `scientific`+`talent` before
  *any* sidecar verdict, which removes the free-exit incentive and raises panel
  coverage). **Caution:** pushing scores up wholesale is grade inflation and defeats
  the screen; the goal is that bands discriminate *within* the population actually
  being screened. Note that rewording `_SCOUT_HUB` guidance strings requires the
  doc-sync update in `docs/specs/2026-08-07-hub-bot-prompts.md` §4 and sign-off
  (per `thread_guidance.py` module docstring); scout_hub has no GM snapshot pin.

**Decision (2026-08-20): R2/R3 are deferred.** Prerequisite work happens now instead:
the rubric — weights, thresholds, anchors, gating, red flags — is extracted into one
standalone document (§4) so that when calibration does happen it is a reviewed edit to
that document. R1 ships with the page work (§2). The `rubric_version` stamping in §4
exists precisely so pre- and post-calibration assessments stay comparable.

---

## 2. Plan: assessments page UX

### Current state (read from `templates/admin/_assessments_body.html`)

The page is one table where **every row is followed by an always-expanded full-width
detail row** — rationale paragraph, de-risking milestone list, and all 13 scores inline —
which is the wall of text being complained about. Other verified issues: the intro copy
says "nine dimension scores" (it's thirteen); band "PASS" renders in gray uppercase next
to the score with no hint it means *decline*; a `route-to-incubation` row still shows
"PASS", which reads as a contradiction; headline cards count by band only; there is no
link from an assessment to its interview thread (although `channel_name` + `slack_ts`
are populated on 29/29 rows) and no per-assessment page. The existing amber drop-banner,
orange incomplete-panel banner, and the collapsible dimension-distribution panel are
good and stay.

### Changes

1. **Collapse the detail rows.** Default collapsed; expand per row (reuse the inline
   `classList.toggle` pattern `_discussions_threads.html` already uses, or `<details>`),
   plus an "expand all" control. This alone removes most of the text wall.
2. **Recommendation becomes the verdict column.** Colored chips:
   `advance` green, `conditional` amber, `route-to-incubation` blue/teal,
   `pass` gray with label **"pass (decline)"**. Score+band become a secondary
   "rubric score" column: `2.55 · below bar`, with the legend explaining that band
   is the computed Part-C.3 banding and "pass" = pass *on the deal*.
3. **Headline cards count by recommendation** (Advance / Conditional /
   Route-to-incubation / Pass-decline). Keep band distribution inside the
   dimension-distribution panel. Fix the "nine dimensions" copy → thirteen.
4. **Red flags column → count badge** ("3 flags"), full list in the detail view.
5. **Per-assessment detail page** `/admin/assessments/{id}` (manager wrapper too):
   full rationale, milestones, gating, red flags, 13 scores as weighted bars, confidence,
   panel status (`panel_incomplete` / `missing_domains` tri-state), raw verdict JSON in
   a collapsed block, and links to: the interview thread (via `channel_name`+`slack_ts`),
   and the specialist panel for that interview (§3). Keep `_assessments_body.html` free
   of absolute admin/manager URLs (the `test_reachability.py` wrapper pattern).
6. **Sort/filter controls**: by score, recency, recommendation, lab; keep default
   score-desc triage order (NULLS LAST as today).

Manager caveat: managers deliberately get no LLM-call drill-down. The detail page for
managers should show verdict substance (scores, rationale, gating, panel domains and
signals) but not raw prompts/responses — flag if specialist *opinion text* should count
as substance or as drill-down (recommend: show signal + concerns list, not raw text).

---

## 3. Plan: specialist responses and tool calls, visible in threads

### Current state (verified)

- Specialist consults happen mid-interview as tool calls; each consult is its own LLM
  call logged to `llm_call_logs` with `phase='consult_<domain>'` (239 rows) — but with
  `channel=NULL` and **no thread/assessment linkage**, and the opinions never appear in
  any UI. In Slack and in `agent_messages`, specialists are completely invisible.
- The which-domains-were-consulted state lives **only in engine memory**
  (`SimulationEngine._specialist_consults`); a restart makes the floor unverifiable
  (that's exactly the `missing_domains=[]` "unverified" state).
- The full tool conversation of every hub turn — `tool_use` blocks (tool + input) and
  `tool_result` blocks (including complete specialist opinions) — **is already durably
  captured** in `llm_call_logs.messages_json` for `thread_reply` rows (608 of the hub's
  726 thread_reply rows contain tool_use), and those rows *do* have `channel` set. So
  retroactive extraction is possible.
- Specialist opinions are structured JSON: `verdict_signal` (blocking/caution/clear),
  `concerns[]`, `questions_to_ask[]`, `confidence` — ideal for compact cards.

### Changes

**Data layer (do first):**

1. **New table `specialist_consults`** — one row per successful consult: `id, simulation_run_id,
   agent_id` (hub), `subject_agent_id, thread_id, channel_name, domain, question,
   context_excerpt, verdict_signal, confidence, concerns (jsonb), questions_to_ask
   (jsonb), raw_opinion (text), created_at`. Written from
   `_execute_consult_specialist`'s success path (the same place `on_consult` fires, so
   "counts for the floor" and "is recorded" can never disagree). Second benefit:
   `_specialist_floor_gap` can consult the table as a fallback, making the floor
   survive restarts and retiring most of the `missing_domains=[]` unverifiable state.
   *Migration numbering:* CLAUDE.md reserves "0030" (unwritten) for the `users.is_admin`
   drop — take the next sequential revision, keep single-head (`scripts/ci.sh` gates it),
   and update the stale doc reference in the same change.
2. **Thread linkage for consult LLM logs going forward:** pass `channel` (and thread ts)
   in the consult's `log_meta` so `llm_call_logs` consult rows stop being orphans.
3. **Optional backfill** for the existing 239 consults: parse `tool_use`/`tool_result`
   pairs out of hub `thread_reply` `messages_json`, join to the thread via the row's
   `channel` + matching the logged `response_text` to the posted `agent_messages`
   content; write `specialist_consults` rows marked `inferred`. Worth doing only if
   the existing 29-assessment corpus needs the new UI retroactively.

**UI layer:**

4. **Interview timeline on the assessment detail page (§2.5):** the thread's
   `agent_messages` interleaved chronologically with **panel cards** (domain, signal
   badge — blocking red / caution amber / clear green — the hub's question, and the
   opinion expandable: concerns, questions_to_ask, confidence) and **tool chips**
   (`search_prior_art("TFEB melanoma") → 0 US title hits`, `retrieve_abstract(...)`),
   expandable to the full result. This is the "what did the hub actually do, and who
   said what" view the current pages lack entirely.
5. **Discussions pages:** add a per-thread indicator (e.g. "panel: 🧪 sci ⚠ · chem ⛔")
   in the threads table and the same timeline in the expanded row, so specialist
   activity is visible even for interviews that ended with no assessment.
6. **LLM calls page:** group `consult_*` rows with their parent interview / add a
   signal badge, so the raw log stops being the only (and unnavigable) record.
7. **Slack-side visibility — recommend against.** Posting specialist opinions into the
   Slack thread would expose internal panel machinery and candid critique to PIs and
   conflicts with the sidecar-confidentiality design ("the sidecar is for Blackbird
   staff, not something to reference or tease"). If Slack visibility is wanted, the
   safe shape is a mirror into a private staff-only channel. Decision needed.

---

## 4. Plan: the rubric as a standalone, reviewable document

**Goal (user decision, 2026-08-20):** the rubric must exist as one separate document a
human can review and adjust — weights, band thresholds, scale anchors, gating criteria —
without touching Python or hunting through prompt files. Calibration (§1.4 R2/R3) is
deferred until this exists.

### 4.1 Where the rubric lives today — nine copies, four load-bearing (verified)

| Copy | Role | Drift risk |
|---|---|---|
| `src/services/blackbird_rubric.py` `RUBRIC_WEIGHTS` + `_BAND_THRESHOLDS`/`band()` | **authoritative for scoring** | — |
| `src/services/directory.py`, `src/agent/simulation.py` | import the above | none (follow code) |
| `src/agent/specialists.py` `maps_to_dimension` | names 8 of the 13 dimension keys | manual |
| `prompts/roles/scout_hub/agent-system.md` §"Blackbird's Screening Rubric" | full prose: gating, funnel, 13-dim table **with weights and anchors**, checklist, red flags, banding, heuristic | manual — duplicate weights table |
| `prompts/roles/scout_hub/phase4-thread-reply.md` | `<assessment_json>` skeleton's 13 score keys, gating keys, "40% science" claim | manual |
| `templates/admin/assessments.html` intro copy | banding legend | already drifted ("nine dimensions") |
| `data/Blackbird_initial_priorities-criteria_v1.pdf` | original source | read-only reference |
| `profiles/private/blackbird.md` (untracked) | legacy transcription, unread at runtime | archive-and-diff owed |

An adjustment today means editing the code dict *and* the prompt table *and* keeping the
skeleton and specialist mapping consistent by hand. That is the thing this section removes.

### 4.2 Design

**New file `prompts/rubric/blackbird-rubric.toml` — the single source of truth.**
Sections: `[meta]` (version, date, source-PDF pointer, changelog note), `[banding]`
(thresholds 4.0/3.0, labels, and the "pass = pass ON the deal (decline)" vocabulary
note), `[gating]` (the three criteria with descriptions), `[[dimension]]` × 13 (`key`,
`weight`, `title`, `anchors` — the "what to look for" prose, with room for per-stage
anchor examples when calibration happens — and `specialist` domain), plus `[funnel]`,
`[red_flags]`, `[checklist]`, `[heuristic]` prose.

**Why TOML, not markdown:** `tomllib` is stdlib and already the repo's manifest
convention (`prompts/roles/*/role.toml`); TOML allows inline comments (the review
medium), multi-line strings keep anchors readable as prose, and there is no fragile
markdown-table parsing. The markdown the hub actually sees is *generated* from it, never
hand-maintained.

**Loader.** `blackbird_rubric.py` parses and validates the TOML: exactly 13 unique keys,
integer weights summing to 100, thresholds on the 0.01 display grid (the existing
module-load assert moves into the validator), anchors non-empty. The module keeps
exporting `RUBRIC_WEIGHTS` / `weighted_score()` / `band()` under the same names, so
`directory.py` and `simulation.py` need no changes.

**Load-once at import, fail-fast at startup — not live reload.** One run = one rubric:
mid-run weight shifts would make scores incomparable within a run, and a half-saved edit
must never become a scoring incident. The startup banner prints the rubric version and a
content hash (matching the repo's banner-as-tell culture). Both `blackbird-app` and the
agent container bind-mount `./prompts` (verified in `docker-compose.prod.yml`), so
**applying an edit is a restart, not an image rebuild**.

**Prompt injection.** The hardcoded rubric section of `agent-system.md` is replaced by a
`{rubric}` placeholder rendered by a `render_rubric_markdown()` next to the loader, using
the same `str.replace` templating `_render_identity` uses (prompt files may contain bare
braces — never `str.format`). First render is kept byte-similar to today's section so the
extraction is behavior-neutral.

**Sync tests instead of templating for the small surfaces** (the repo's
`test_doc_prompt_sync.py` pattern): the phase4 skeleton's `scores` keys must equal the
TOML keys; every `specialists.py` `maps_to_dimension` must be a TOML key; the science
weight share quoted in phase4 ("40%") must match the TOML sum. The JSON skeleton stays a
literal in the prompt — it is format-sensitive and better reviewed in place.

**Page copy from the same source.** The assessments-page legend (thresholds, labels,
dimension count) renders from the TOML — retiring the stale "nine dimension scores" class
of bug permanently.

**`rubric_version` stamping (recommended).** Add `rubric_version` (from `[meta]`, plus
content hash) to new `opportunity_assessments` rows — rides the same migration as
`specialist_consults` (§3.1). This is what will make pre-/post-calibration rows
distinguishable when §1.4's deferred work happens; without it the future calibration
cannot be evaluated against the old data.

**One-time chores in the same change:** diff the untracked `profiles/private/blackbird.md`
against the TOML, fold in anything never migrated, archive the file (retires the standing
deploy-checklist item); update CLAUDE.md's "rubric criteria live in
phase4-thread-reply.md" pointer to the new file.

### 4.3 The edit workflow this buys (the point)

1. Edit `prompts/rubric/blackbird-rubric.toml` — weights, anchors, thresholds, with
   comments explaining each choice.
2. `./scripts/ci.sh` — the validator and sync tests are the review gate; a
   characterization test pins the *initial* TOML to today's exact weights/thresholds, so
   the extraction itself is provably neutral and any later change is a deliberate,
   reviewed test update.
3. Restart `blackbird-app`/`worker` and stop/start `blackbird-agent-run` (graceful,
   `-t 420`) — no rebuild, since `prompts/` is mounted. Banner confirms version + hash.
4. New assessments carry the new `rubric_version`.

---

## 5. Sequencing, verification, deploy notes

**Phase 0 (no schema, no model change):** §2.1–2.4 + copy fixes + "pass (decline)"
labeling. Immediately answers "why does everything say pass".
**Phase 1:** rubric extraction (§4) — behavior-neutral by construction, pinned by the
characterization test; plus `specialist_consults` table + consult `log_meta` linkage
(§3.1–3.2) and `rubric_version` stamping (§4.2) in **one shared migration**;
floor-durability fallback.
**Phase 2:** assessment detail page + interview timeline (§2.5, §3.4–3.6); optional
backfill (§3.3).
**Phase 3 (deferred — decision 2026-08-20):** calibration (§1.4 R2/R3), performed later
as a reviewed edit to `prompts/rubric/blackbird-rubric.toml` once §4 is in place and,
ideally, another run of data has accumulated under `rubric_version` stamping.

Verification: `./scripts/ci.sh` (alembic single-head + round-trip, ruff, pytest w/
coverage floor) — it is the entire gate. New routes need `test_reachability.py` link
credits via the admin/manager wrappers (shared bodies stay URL-free). Template changes
to `_assessments_body.html` are shared by admin and manager pages — check both renders.
Don't touch `_PI_LAB` guidance strings (GM-snapshot-pinned; never `--snapshot-update`).

Deploy: additive migration → migrate before new code serves (`$DC build` → `run --rm
blackbird-app alembic upgrade head` → `up -d`); the consult write path lives in the
**agent image**, which must be rebuilt explicitly (`$DC --profile agent build agent`) —
`src/` is baked in, not mounted. Graceful stop: `docker stop -t 420 blackbird-agent-run`,
save logs, verify exit 0. Note: the agent-run container exited cleanly ~2026-08-19 and
the simulation is **currently not running**; restarting it is an operator decision.

---

## 6. Decisions

1. ~~§1.4: adopt R2/R3 now, or presentation-only first?~~ **Resolved 2026-08-20:
   calibration deferred; extract the rubric into a standalone reviewable document
   first (§4), then calibrate later by editing it.**
2. §2 manager policy: do specialist opinions count as "assessment substance" (managers
   may see) or "LLM drill-down" (managers may not)?
3. §3.3: backfill the existing 239 consults, or forward-only?
4. §3.7: any Slack-side specialist visibility (recommend: none, or staff-only mirror)?
5. §4.2 (small, defaulted unless overridden): load-once-at-import with restart-to-apply
   (recommended, and what the plan assumes) vs live per-call reload of the rubric file.
