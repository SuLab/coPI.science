# Applied red-team corrections — Part 22

Fixer note: a previous fixer run was cut off mid-edit while replacing Task 22.3's body. This pass
first audited every item against the current file state; items already fully and coherently
present are marked "found applied" below (verified, not re-applied) and items still missing were
applied now. Order: blockers, then majors, then anchors/minors.

## Blockers

- **22.2 seed INSERT (BLOCKER-1)** — found applied. The Step-1 scratch-DB seed already names
  `is_admin`, `email_notifications_enabled`, `onboarding_complete`, `access_status` explicitly
  (verified against `alembic/versions/0001_initial.py:33-35` and `0010_access_gate_and_waitlist.py:50`
  — those three are NOT NULL with no server-side DDL default). No change needed.
- **22.3 verify-only (BLOCKER-2)** — found applied. Task body is already the corrected verify-only
  version: Files/Interfaces both "none", Steps 2-6 are all N/A, and the grep table lists all ten
  real hits. Re-ran the grep against the live repo
  (`grep -n '"0024"' tests/integration/test_harness_smoke.py scripts/migrate/preflight.py scripts/migrate/run_migration.sh tests/unit/test_migration_checks.py`)
  and it returns exactly the ten hits the task's table names, at the lines the table gives. No
  change needed.
- **22.7 `Form(None)` (BLOCKER-3)** — applied. Replaced `Form(None)` + `is not None` guards with
  `Form("")` (unchanged) + `"<field>" in (await request.form())` presence checks in
  `profile.py::profile_save`, `onboarding.py::save_profile`, `agent_page.py::save_public_profile`.
  Added the "still clears a field" regression test to all three route test files.
- **22.7/22.8 `world.pi` duplicate profile (BLOCKER-4)** — applied. Both new tests
  (`test_save_public_profile_partial_post_does_not_blank_omitted_fields`,
  `test_save_public_profile_resets_synthesis_validated`) now `select(ResearcherProfile).where(...
  == world.pi.id)` and mutate the row `world` already created via `factories.make_profile`, instead
  of calling `factories.make_profile(..., user=world.pi, ...)` a second time.

## Majors

- **22.4 test cannot fail / ruff F401 (MAJOR-1)** — applied. Extracted `_dedup_pmids()` as a
  module-level helper in `profile_pipeline.py`; the unit test now imports and calls it (fails with
  `ImportError` pre-fix). Added the `existing_pubs`-half GM test
  (`test_a_pmid_listed_twice_by_orcid_inserts_exactly_one_publication`) to
  `tests/characterization/test_profile_pipeline_gm.py`. Re-ran ruff on the corrected unit-test
  snippet: clean (see Verification below).
- **22.4/22.12 snippet indentation (MAJOR-4)** — applied for 22.4 (DOI-resolution loop now shown at
  12 spaces, with an explicit "re-indent to match" note) as part of the 22.4 rewrite above; applied
  separately for 22.12 (see below).
- **22.4 MINOR-5 `.order_by(Publication.id)`** — applied in the same edit (folded in since it
  touches the same corrected snippet): the PMC-methods lookup now uses
  `.scalars().first()` with an explicit `.order_by(Publication.id)`.
- **22.2 preflight/M.1 ownership collision (MAJOR-8)** — found applied. The "Coordinator note
  (reconciliation item 1)" paragraph is present verbatim (owns only the `PLANNED_OBJECTS` append +
  `REVISION_ORDER`/drift-loop extension to "0025"; M.1 owns everything else; warns M.1 the
  PlannedObject tuple entry may already be present).
- **22.2 destructive DELETE with no record (MAJOR-9)** — found applied. `upgrade()` already prints
  `f"0025: deleted {result.rowcount} duplicate (user_id, pmid) publication rows"` and the Deploy
  note already covers the lock footprint / stop-worker-or-retry guidance and the
  `backfill_publications.py` follow-up.
- **22.9 `specs/admin-dashboard.md` left stale (MAJOR-11)** — applied. Added it to Files, added the
  corrected profile-status line (`no_profile|generating|complete` + a note that `pending_update`
  was removed) to Step 3, and to the Step 6 `git add` line. Also fixed the
  `specs/auth-and-user-management.md` replacement, which had wrongly deleted the still-true old
  step 6 ("If no arrays changed: stores new publications but does not bother user") — restored it
  and renumbered 7/8 as explicit removals, matching the report's corrected text exactly.

## Anchors (folded into the same edits above)

- **22.2** `preflight.py :114-206` → left as-is (report marked this MINOR/non-issue: 114 is the
  section-comment line, not the tuple's start; `PLANNED_OBJECTS` truly starts `:168`, closing `)`
  at `:204`, `REVISION_ORDER` at `:206` — all already correct in the task body's own inline
  citations).
- **22.4** Files anchor corrected: `run_profile_pipeline` now cited as "starts `:51`" with the
  actual edit points (`:115`, `:140-150`, `:212-229`, `:271-282`) instead of the misleading
  `:107-282` span. PMC-lookup anchor corrected `:272-282` → `:271-282`.
- **22.9** `admin.py` Files anchor corrected `:118-127` → `:117-127` (verified: `if not profile:`
  is at `:117`). `specs/profile-ingestion.md` anchor corrected `:180-186`/`:184-186` → `:186-187`
  (verified: items f/g are exactly those two lines). `specs/auth-and-user-management.md` anchor
  corrected `:137-144`/`:143-146` → `:144-147` (verified: steps 5-8 are exactly those four lines).
- **22.10** `profile_export.py` second span corrected `:139-147` → `:138-147` (verified: `path =
  PRIVATE_PROFILES_DIR / f"{agent_id}.md"` is at `:138`, not `:139`).
- **22.11** `run_profile_pipeline` anchor corrected from the single `:457-474`/`:457-484` pair to
  three precise sub-spans: Step 9b starts `:457`, agent-id lookup `:468-474`, export block
  `:476-497` (verified against the live file).

## 22.10 (mkstemp mode + vacuous test)

- **`mkstemp` 0600 mode regression + false umask deploy note (MAJOR-5)** — applied.
  `atomic_write_text` now does `os.chmod(tmp_name, stat.S_IMODE(os.stat(path).st_mode))` (falling
  back to `0o644` for a brand-new target) before `os.replace`; added the fifth unit test
  (`test_the_targets_permissions_survive_the_replace`); corrected the Deploy note to state the true
  `mkstemp` behavior instead of the false umask claim.
- **Vacuous "first private profile save" test (MAJOR-6)** — applied. Replaced the `world.pi`-based
  test (which already has a `ResearcherProfile` with `private_profile_md` set via the `world`
  fixture, so it exercised the pre-existing `if profile:` path and passed unmodified pre-fix) with
  one built on `_agent_for` (profile-less user + agent), matching the report's corrected test
  exactly.
- **MINOR-13** (task's "every disk writer in `src/`" claim vs. `foa_cache.py`/`grantbot.py` being
  out of scope) — applied: reworded the task's opening paragraph to name the three actual adoption
  sites and explicitly note the two excluded `write_text` calls and why (`V6-24b` scope, and
  `grantbot.py` is Part 23's file).
- **MINOR-12** (docstring says "a crash", guarantee is only process-crash not machine-crash/power
  loss, no fsync) — applied: tightened the module docstring wording rather than adding an `fsync`
  (out of the finding's scope, which is about truncation, not power-loss durability).
- **MINOR-11** (global `os`/`tempfile` monkeypatch scope; leaked-temp-file gitignore gap) —
  applied: added a one-line comment in the test accepting the global-patch tradeoff (with the
  `SimpleNamespace` alternative named), and added `profiles/**/*.tmp` to `.gitignore` (new Files
  entry, new Step-3 edit, added to the Step-6 `git add` line).

## 22.11 (placeholder GM test)

- **Placeholder GM test naming a nonexistent fixture (MAJOR-7)** — applied. Replaced the
  `export_dirs`/`agent`-placeholder test with the report's concrete, self-contained
  `test_first_run_exports_the_private_seed_to_disk` (patches `profile_export.PROFILES_DIR`/
  `PRIVATE_PROFILES_DIR` directly, creates its own `AgentRegistry` via `factories.make_agent`,
  reuses `_install_fakes`/`_PRIVATE_SEED` already in the file).

## 22.12 (`apply_synthesis` gate — `return 1`, indentation, MINORs)

- **`return 1` in a `-> bool` function (MAJOR-3)** — applied. `regen_profiles_from_web.py`'s
  snippet now returns `False` on the validation-gate-skip path (verified: `regen_one` is
  `async def regen_one(...) -> bool`, returning `False` on every other failure path). Left
  `regen_profile_from_cv.py`'s `return 1` alone and said why (`_run` there returns `int`).
- **Snippet indentation 4 spaces short (MAJOR-4)** — applied to `regen_profile_from_cv.py` (fixed
  as part of the 22.4/22.12 shared correction) and `regen_profiles_from_web.py` (both re-indented
  to the real 8 spaces, nested inside `async with ... as db:`); confirmed `vet_publications.py`
  (12 spaces) and `resynth_from_current_pubs.py` (4 spaces) were already correct as shown, per the
  report — no change made to those two.
- **MINOR-17** (pipeline discards `apply_synthesis`'s return value while still bumping
  `profile_version`/evidence counts) — applied: wrapped the three dependent lines in
  `if apply_synthesis(...):` in the Step-9 "else" branch; functionally a no-op today (the case
  analysis holds), purely a readability/invariant guard.
- **MINOR-15** (four scripts import the module-private `_validate_profile`; make it public or have
  `apply_synthesis` compute it) — **skipped**: both options are redesigns (renaming a public
  contract across 5 call sites, or changing `apply_synthesis`'s signature/contract) that ripple
  beyond this task's diff; left as-is.
- **MINOR-16** (`datetime`/`timezone` imports become unused in the four scripts after this task
  drops the manual `profile_generated_at` assignment; `scripts/*.py` is outside `LINT_TARGETS`, so
  cosmetic only) — **skipped**: the task's own snippets don't show each script's full import block,
  and enumerating/trimming four scripts' unrelated imports is out of proportion to a
  ruff-invisible, cosmetic finding; noted here instead so it isn't lost.

## 22.13 (ruff F401 + source-grep-only test)

- **Unit test ruff F401 + source-grep cannot detect a wrong statement (MAJOR-2)** — applied.
  Split `bump_profile_version_stmt(profile_id) -> Update` (pure) out of the async
  `bump_profile_version`; the corrected test compiles the real statement against
  `postgresql.dialect()` and asserts on the exact SQL string, using both `uuid` and
  `sqlalchemy.dialects.postgresql` (so neither import is unused). Verified: `str(...compile(...))`
  produces exactly `UPDATE researcher_profiles SET
  profile_version=(coalesce(researcher_profiles.profile_version, 0) + 1) WHERE
  researcher_profiles.id = '<uuid>' RETURNING researcher_profiles.profile_version`, matching all
  three of the test's assertions.
- Updated the pipeline call site to the `if apply_synthesis(...):`-nested form to match 22.12's
  MINOR-17 fix.
- **MINOR-18** (`profile.profile_version = await bump_profile_version(...)` re-dirties the ORM
  attribute) — applied: added the explaining comment to `bump_profile_version`'s docstring.
- **MINOR-19** (module-level import of `bump_profile_version` in three routers drags
  `src.services.llm`/`orcid`/`pubmed` into the web app's import graph) — applied the cheaper option
  (keep the function in `profile_pipeline.py`, fix only the import ordering so ruff `I001` doesn't
  flag it) with a "Coordinator note" explaining why the `profile_versioning.py` move was rejected
  as out-of-proportion for a MINOR; specified the exact alphabetical insertion point in all three
  routers (`profile.py`, `onboarding.py`, `agent_page.py`).

## 22.14 (`invite.py` handler rewrite)

- **`invite.py` snippet silently rewrites the existing `except` handler (MAJOR-10)** — applied.
  Reproduced the real `except Exception as exc:` block verbatim (`:245-252`, comment about the
  bare-`pass`-hid-an-ImportError history plus the `"Delegate Slack-ID sync failed for agent %s:
  %s"` log line) instead of the differing block the first-pass snippet showed; only the `try` body
  changes. Added a note to re-verify via `sed -n '245,252p'` before committing rather than
  retyping from memory.

## Anchors — remaining items (22.1, 22.5, 22.8, 22.9 spec doc, one 22.14 leftover)

- **22.1** `_record()` fixture span and "17 existing tests" — found already applied (the file
  already read `_record()` (`:22-57`)` and "12 existing tests + the 4 new ones ... = 16" before
  this pass touched anything).
- **22.5** `_parse_pubmed_xml` span corrected: Files-list anchor now cites the title block
  (`:206-208`) and abstract block (`:209-219`) separately instead of one `:207-219` span (verified
  against the live file: `# Title` comment at `:206`, `record["abstract"] = ...` at `:219`).
- **22.8** `_prof` span corrected `:166-179` → `:166-181` in all three occurrences (verified: the
  real helper's closing `.mappings().first()` is at `:181`, not `:179`).
- **22.14** caught one leftover stale anchor the first correction pass missed: `agent_page.py`'s
  connect-slack Step-3 code-block header still said `:1422-1430`; corrected to `:1419-1428` to
  match the Files-list entry fixed earlier in this same task.

## Coverage matrix / Files list / Open decisions (instruction F)

- **Coverage matrix, `V6-form4` row** — updated: no longer describes the fix as "switching
  `institution`/`department` to `Form(None)` too" (that was the rejected naive fix); now describes
  the shipped `"<field>" in (await request.form())` presence-check gating applied to all six
  fields uniformly.
- **Files list** — added `.gitignore` (22.10's new `profiles/**/*.tmp` entry) and
  `specs/admin-dashboard.md` (22.9's new edit); corrected the `scripts/migrate/preflight.py` entry
  to note it is `PLANNED_OBJECTS`/`REVISION_ORDER` only; removed `tests/integration/test_harness_smoke.py`
  and `scripts/migrate/run_migration.sh` from the "modifies" list — neither file is edited by any
  task in this part after 22.3 became verify-only (both are now M.1's, per reconciliation item 1).
- **Open decision 3** — rewritten: the old text asked the coordinator to resolve a
  `HEAD_REVISION` placeholder that Task 22.3 no longer introduces (superseded by the BLOCKER-2 fix
  and cross-part reconciliation item 1, which gives M.1 sole ownership of every head pin). The
  entry now states there is nothing left for this part to decide here and points to M.1's landing
  + Task 22.3's grep re-verification as the actual precondition.

## Anchor rows confirmed OK — no change made (part of the same 19-row sweep)

Checked against the live repo and left untouched because the report itself found them exact or
non-issues: 22.2 `publication.py :13-41`; 22.2 `preflight.py :114-206` (114 is the section
comment, not a real drift); 22.6 `_validate_profile :553-579` / retry prompt `:324` / progress
text `:429-434`; 22.7 all three routers' signature/body spans; 22.9 `specs/data-model.md :53-60`;
22.13 the four C1-a sites; 22.11 `agent.py:119-126`'s default string.

## Tests re-run (DB-free, `.venv-test/bin/python`, from a scratch project root mirroring
`pyproject.toml`'s ruff config so first-party import sorting resolves correctly)

- `tests/unit/test_profile_pipeline_dedup.py` (22.4, corrected) — ruff clean.
- `tests/characterization/test_profile_pipeline_gm.py` with both new tests appended (22.4's
  `test_a_pmid_listed_twice_by_orcid_inserts_exactly_one_publication` and 22.11's
  `test_first_run_exports_the_private_seed_to_disk`, together) — ruff clean.
- `tests/integration/test_onboarding_flow.py` with all four new/changed 22.7/22.8 tests appended
  (`test_profile_save_partial_post_does_not_blank_omitted_fields`,
  `test_onboarding_save_profile_partial_post_does_not_blank_omitted_fields`,
  `test_profile_save_still_clears_a_field_the_user_emptied`,
  `test_onboarding_save_profile_still_clears_a_field_the_user_emptied`) — ruff clean.
- `tests/integration/test_agent_page.py` with all five new/changed tests appended (22.7's two
  `save_public_profile` partial/clears tests, 22.8's `synthesis_validated` reset test, 22.10's
  `test_first_private_profile_save_creates_the_missing_profile_row`, 22.14's
  `test_removing_a_delegate_also_removes_their_slack_id`) — ruff clean.
- `tests/unit/test_atomic_write.py` (22.10, with the new 5th mode-preservation test) — ruff clean;
  **ran all 5 against the corrected `atomic_write_text` implementation: 5 passed**, including the
  new permissions test (`0664` survives the replace).
- `tests/unit/test_apply_synthesis.py` (22.12, unchanged by this pass) — ruff clean; **ran the two
  gate-boundary cases by hand against the module logic: both correct** (first-ever unvalidated
  synthesis stores; a validated stored profile rejects an unvalidated overwrite).
- `tests/unit/test_bump_profile_version_sql.py` (22.13, corrected) — ruff clean; **ran the
  compiled-SQL assertions against the real statement shape: all three pass**, SQL matches the
  report's quoted output exactly.
- `tests/unit/test_delegate_slack_ids_sql.py` (22.14, unchanged by this pass) — re-ruff-checked
  alongside the new module file: clean (no regression from other edits in this part).
- Re-ran `grep -n '"0024"' tests/integration/test_harness_smoke.py scripts/migrate/preflight.py scripts/migrate/run_migration.sh tests/unit/test_migration_checks.py`
  against the live repo for Task 22.3: got exactly the ten hits the corrected task's table lists,
  at the lines the table gives.

## Summary

All 4 blockers, all 11 majors, and every anchor drift the report enumerated were either found
already applied (22.2's seed INSERT, 22.3's full verify-only rewrite, 22.1's two anchor items,
22.2's Coordinator-note and DELETE-count MAJORs) or applied fresh in this pass. 2 MINORs were
skipped as redesigns (22.12 MINOR-15, MINOR-16) and are listed above with reasons. Coverage
matrix, Files list, and Open decisions were updated where the corrections changed them (2.7's
`V6-form4` note, 22.3's `HEAD_REVISION` open decision, the Files list's `.gitignore` /
`specs/admin-dashboard.md` additions and `run_migration.sh`/`test_harness_smoke.py` removal). Task
22.3 is now a single coherent verify-only task with no duplicated or half-replaced text.
