# Post-implementation adversarial audit — specialist panel remediation

**Date:** 2026-08-19
**Branch:** `feat/specialist-panel-remediation` (15 commits, `a7acd72..d92ca76`)
**Spec:** `docs/specs/2026-08-18-specialist-panel-remediation-design.md`
**Plan:** `docs/plans/2026-08-18-specialist-panel-remediation.md`
**Method:** every claim below was re-derived by the auditor from code, from the 18 real
production verdicts, or from a live command — not taken from any implementer or reviewer report.

## Verdict

**The branch does what the spec says, and does not do what the spec says it defers.**
No Critical findings. The gate passes independently. The prompt freeze held byte-for-byte.
Two real limitations remain and are recorded here rather than papered over.

## 1. Independent gate run

`./scripts/ci.sh`, run by the auditor rather than trusted from a report:

```
2071 passed, 93 skipped, 1 warning in 387.39s
Required test coverage of 60% reached. Total coverage: 76.13%
16 snapshots passed.
==> CI passed.
```

The 16 passing snapshots matter specifically: `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`
pins the `pi_lab` guidance strings, so a green snapshot report is direct evidence no PI-bot
behaviour drifted. `pytest --snapshot-update` was never run.

## 2. The prompt freeze (spec decision D6)

Verified two independent ways.

| Check | Result |
|---|---|
| `git diff --stat a7acd72..HEAD -- prompts/ src/agent/thread_guidance.py src/services/blackbird_rubric.py` | **empty** |
| md5 of all 19 frozen files vs a baseline captured *before* any work began | **identical** |
| `consult_specialist` block in `TOOL_DEFINITIONS`, md5 old vs new | `bf867339…` == `bf867339…` |
| `RUBRIC_WEIGHTS` / `_BAND_THRESHOLDS` | **unchanged** — the 18 production scores stay comparable |
| Control flow in `_reply_to_thread` | unchanged: `_post_message` (:1733) still precedes `_capture_hub_assessment` (:1776) |

## 3. Finding-by-finding, re-derived

| # | Status | Evidence the auditor produced |
|---|---|---|
| F1 | **Diagnosed, not "fixed"** | `clear` IS reachable — the scientific persona returned it on a clean synthetic case. So the 142/142 caution/blocking result is a fact about the ideas, not a persona defect. All 8 persona files are byte-identical to baseline. |
| F2 | **Recorded, not resolved** | The floor still cannot *convene* a panel that was skipped; it can only record the gap. Honest — the pre-post gate that would fix it is deferred by D6. |
| F3 | **Fixed** | A gapped verdict is stored and flagged, never discarded. `assessment_kwargs` is one dict shared by the first write and the `_pending_assessments` retry, so the flag cannot be lost on retry. |
| F4 | **Fixed** | All 7 known false positives now reject (`aso`/reasons, `hit`/architecture, `als`/also·signals·animals·journals, `compound`/compounding); all 11 genuine forms still match, including hyphenated and stem cues. |
| F5 | **Deferred, verified not-done** | Exhaustive probe: `commercial` and `budget` remain unrequirable under every input. |
| F6a | **Deferred, verified not-done** | `legal` still keys on `gating.fto_achievable == "met"`. |
| F6b | **Deferred, verified not-done** | No `ip_fto` special-case anywhere in `src/`. |
| F7 | **Deferred, verified not-done** | Zero references to specialist opinions in the phase-4 prompt path. |
| F8 | **Partially instrumented** | `dimension_stats`, `band_counts`, `incomplete_panel_count` in `list_assessments`; clear-rate monitor at `simulation.py:845-848`. No scoring logic changed. |
| F9 | **Fixed** | 14/14 cases correct. Empty/whitespace/`null`/`[]`/`{}`/scalars excluded; **prose still counts** (the design decision that must not be reversed); a populated object with unrecognised keys now counts too. |
| F10 | **Fixed** | `(pi, thread_id)` keying: a second interview inherits nothing. |
| F11 | **Fixed** | `maps_to_dimension` has its first runtime read at `directory.py:232`; a ratchet test pins the two hand-typed domain lists against each other. |
| F12 | **Closed by deletion** | Both surviving `_record_assessment_drop` sites (`:1781`, `:2595`) pass `thread_id`. The site that lacked it was removed by Phase 1. Spec §9 now says this rather than claiming provenance was added. |

### F4, measured on the 18 real production verdicts

Exactly four rows change, all *losing* a spuriously-demanded specialist; none gains one:

| row | subject | change |
|---|---|---|
| 5 | `pearce` | loses `chemistry` (`hit` inside "architecture") |
| 8 | `hart` | loses `chemistry` (`aso` inside "reasons") |
| 12 | `mcmeniman` | loses `clinical` (`als` inside "animals") |
| 17 | `coller` | loses `chemistry` (`compound` inside "compounding") |

The other 14 rows are unchanged — including the *other* `pearce`, `hart` and `huganir` rows,
which is the check that distinguishes a targeted fix from a blunt one.

## 4. The three-state row (the final review's central finding)

The whole-branch review caught a defect no per-task review could see: after Phase 1 removed the
refusal, the *rationale* for failing open was stale — and worse, a fail-open row was
indistinguishable from a verified-complete one. Since the production container's last exit was
`SIGKILL (137)`, that path is not hypothetical.

Auditor-verified behaviour, all five cases:

| Situation | gap | verifiable | `missing_domains` |
|---|---|---|---|
| Panel verified complete | `[]` | True | `NULL` |
| Panel verifiably gapped | `[names]` | True | `[names]` |
| **Post-restart, nothing armed** | `[]` | **False** | **`[]`** |
| Verdict names no subject | `[]` | **False** | **`[]`** |
| `pass` (needs no panel) | `[]` | True | `NULL` |

`panel_incomplete` stays `False` for the `[]` case — deliberately. Flagging it would false-flag
every resumed thread whose panel genuinely *was* convened. `incomplete_panel_count` is therefore
documented as a lower bound.

## 5. Production data safety

Nothing in this work touched the live database. Verified live:

```
assessments=18   drops=3   alembic=0028
```

Production is still on `0028` — i.e. **not yet migrated**. That is correct and intended: the
deploy has not happened. Migration `0029` chains cleanly off `0028`, single head, round-trips in
`ci.sh`.

## 6. What remains — stated plainly

1. **The clear-rate monitor is a shutdown-time log warning, not an admin surface.** Spec §10
   originally promised a read-only admin card. It fires only on a graceful `stop()`, and the
   production container's last exit was `SIGKILL`, so on the evidence it may never fire. The spec
   has been amended to say this, and the DB-backed card is a named follow-up — the source data is
   already durable in `llm_call_logs WHERE phase LIKE 'consult%'`, so it is cheap to add. **Do not
   treat "no warning" as evidence of a healthy panel until that card exists.**
2. **F2 is recorded, not resolved.** A skipped panel stays skipped; the floor records the gap but
   cannot convene the panel. The pre-post gate that would fix it needs hub-facing prompt text,
   which D6 forbids.
3. **F5/F6a/F6b/F7 remain deferred by design**, each verified as genuinely not-done rather than
   silently half-done. They need a follow-up that ships code and prompt together.
4. **Revision-number hazard.** The unmerged branch `feat/user-account-types-0029` also claims
   revision `0029`. Ours is valid on this branch today; whoever merges that branch must renumber it
   to `0030` (which `CLAUDE.md` now reserves). `ci.sh`'s single-head check fails loudly on
   collision, so this cannot land silently.
5. **Deferred minors**, each judged ACCEPTABLE by the final review: the in-memory signal tally is
   lost on restart; `dimension_stats` is computed from the 500-row score-ordered slice (identical
   to the pre-existing band cards); the `{% if dimension_stats %}` guard is always true;
   `test_opportunity_models.py` asserts nullability but not column type.

## 7. Deployment

`src/` changed, so **the agent image must be rebuilt** — it bakes `src/` at build time and does not
mount it. Migrate before the new code serves, since the model maps both new columns:

```bash
DC="docker compose -f docker-compose.prod.yml"     # never bare `docker compose`
$DC build blackbird-app worker
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current          # must equal `alembic heads`
$DC up -d blackbird-app worker
$DC --profile agent build agent                     # not optional
```

Never pass `--remove-orphans`. The simulation container is `Exited (137)`; restarting it is an
operator decision and is not part of this work.

## 8. Incident: near-miss with untracked files

During the final fix wave an implementer ran `git stash -u` to measure a lint baseline. It failed
partway and removed 19 **untracked** files — work not in version control (`docs/audits/`,
`docs/plans/`, `docs/specs/`, `logs/`, `SECOND_INSTANCE_SETUP.md`, `scripts/make_install_links.py`,
`slack_install_links.md`). The agent recovered them from the stash's third parent.

The auditor verified recovery independently rather than accepting the claim: all 18 named files
present with intact sizes, `logs/` still holding its 16 files matching the session-start baseline,
the 3 modified tracked files still modified, and `git stash list` containing exactly two stashes
both dated 2026-08-14/15 — pre-existing, days before this work. **Nothing was lost.**

Root cause is partly a briefing gap: dispatches banned docker commands and background jobs (the two
hazards that had already caused failures) but did not ban destructive git operations. Any future
dispatch in this repo should forbid `git stash`, `git clean` and `git checkout .` explicitly, because
the repo carries untracked user files that are unrecoverable.
