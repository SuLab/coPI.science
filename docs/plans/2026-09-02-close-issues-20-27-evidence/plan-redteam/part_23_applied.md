# Changelog — red-team corrections applied to plan/parts/part_23.md

Source report: plan/redteam/part_23_24.md. Order: blockers, then majors, then minors.

## Blockers

- B1 (23.1): Replaced `from datetime import datetime, timezone` / `timezone.utc` with
  `from datetime import UTC, datetime` / `UTC` in both `src/agent/retry_after.py`'s Step-3 module code and
  `tests/unit/test_retry_after.py`'s `test_an_http_date_in_the_future_is_honoured_and_capped` (Step 1).
  Added a `ruff check src/agent/retry_after.py tests/unit/test_retry_after.py` line to Step 5 with the
  rationale (UP017, zero-tolerance in tests/, ratchet headroom in src/). Also corrected Step 4's stated
  `test_slack_client_contract.py` count from the stale baseline "64 passed" to "67 passed" per M6.
- B2 (23.4): Replaced the env-only remediation with the report's DB-first implementation
  (`get_agent_bot_token(session, "grantbot")` then `.env` fallback, `is_valid_token` gate). Rewrote Context,
  Open decision and Deploy note to match COORD_B.md Decision D10 (confirm a token exists before deploy; DB
  token needs no restart, `.env` token needs `up -d --force-recreate grantbot`). Files list anchor corrected
  to `:615-627` (verified against the real tree).
- B3 (23.4): Moved the regression test out of `tests/integration/test_grantbot_live.py` (module-level
  `pytestmark = [pytest.mark.integration, pytest.mark.live_api]`, unconditionally skipped by
  `tests/conftest.py:162-166` without `LIVE_API_TESTS=1`, which `./scripts/ci.sh` never sets) into a new file
  `tests/integration/test_grantbot_token_routing.py` marked plain `pytest.mark.integration`, so it actually
  runs as part of the gate. The new file imports `_ExplodingWebClient`, `_StageRecorder` and
  `synthetic_opportunity` from `test_grantbot_live.py`, mirroring the in-repo precedent
  `tests/integration/test_conversation_feed.py:16` (`from tests.integration.test_agent_page import _auth`).
  Re-derived the test body to drop the now-unneeded `sim_run` fixture (unused by this code path — no post
  ever reaches `_post_funding_to_db`/`get_latest_run_id` since the token check aborts first) and to inline
  the `now_utc`/`fixed_catalogue`/`_isolate_foa_cache` fixture equivalents (redirecting `foa_cache.CACHE_DIR`
  via `tmp_path`, since `cache_foa` writes before the token check per `grantbot.py`'s step 4b — verified by
  reading `grantbot.py:571-580`). Added the `createdb copi_x23` step to Step 2/4's command per the report.
  Dropped `-m "integration and live_api"` per the report (redundant / hides typos) — moot now since the new
  file carries no `live_api` marker at all.

## Majors

- M1 (23.8): Lowered `_ACK_SUBSTANTIVE_WORD_COUNT` from 12 to 10 (the report's measured-safe value — the
  longest `TestAcknowledgmentOnly.test_positive_cases` fixture is 6 words); added COR-28b's own 11-word
  reproduction (`findings/issue_23.md:30`, "Agreed, we can send the plasmids and the mice next week.") as a
  parametrized case alongside the original longer invented sentence, since a 12-word threshold never fires
  on it and the task did not close its own finding. Updated Context, Step 1/2/3/4/5 text and counts (35→37,
  not 35→36), and replaced the stale "Open Decision 3" language with the report's superseding note. Also
  fixed the `Files:` anchor (`def` at :108, not :104) and, as a related Minor 4 anchor fix, corrected Task
  23.7's Step 1/Files to show a BEFORE/AFTER of `TestAnnouncementOnly.test_positive_cases` (:35-44) only,
  instead of re-opening the whole `class TestAnnouncementOnly:` (which visually deleted
  `test_negative_cases` :46-61 and `test_mixed_announcement_with_substance_allowed` :63-70).
- M2 (reconciliation item 2 / 24.2): **out of scope for this pass** — the correction targets
  `plan/COORD_A.md`'s cross-part reconciliation table (item 2), not `parts/part_23.md` or `parts/part_24.md`;
  not in the fixer's edit set and not listed in the assignment's majors. Left untouched; flagging here so the
  coordinator applies COORD_A.md's item-2 fix (24.2's guard body/ordering is otherwise correct and needs no
  change on its own — see M2's last paragraph in the report).
- M3 (24.3): applied in part_24.md — see plan/redteam/part_24_applied.md.
- M4 (23.3): Rewrote the `except Exception as exc:` comment in the AFTER block to state the real trade-off
  (returning `[]` lets `_run_grantbot_with_session` finish normally, so the scheduler's
  `_mark_run_complete()` still runs and `_should_run_today()` is False for the rest of the UTC day — a full
  day of lost funding posts, not "the next scheduled run tries again with a fresh LLM call" as the draft
  said) and added a "Coordinator note" after the AFTER block making the same point explicit. Updated the
  Coverage matrix's COR-26b' row with the report's requested sentence: "Task 23.3 *removes* this trigger
  rather than leaving it — after 23.3 a malformed selection no longer re-fires, it skips the day", with the
  causal chain spelled out (pre-fix TypeError crashed the tick before `_mark_run_complete()`; post-fix the
  same input is caught and returns `[]`, so the tick completes normally and `_mark_run_complete()` runs).
- M5 (24.3): applied in part_24.md — see plan/redteam/part_24_applied.md.
- M6 (measured-output corrections):
  - 23.1/23.2: applied above (67 passed, not 64).
  - 23.8: applied above (37, not 36) as part of M1.
  - 23.3: independently re-measured (see Tests re-run) — corrected Step 2 to **3 failed, 1 passed** (not the
    draft's "2 failed, 2 passed", and not the report's "4 failed, 1 passed" either: that figure conflates a
    failure from Task 23.5's `test_dead_profile_search_helpers_are_removed`, a different file, per the
    report's own explanation — running just `tests/unit/test_grantbot_selection.py` in isolation, as the
    task's own Step-2 command does, gives 4 tests total and 3 fail/1 passes). Corrected the claim that the
    invented-number case "already passes today" — it does not; pre-fix there is no membership filter at all.
  - 23.6: corrected Step 4's `test_foa_pattern.py` count from "15 passed (12 family/case rows...)" to
    "18 passed (14 family/case rows...)" — re-verified the parametrize list is 7 pairs plus the 2 four-digit-
    year rows = 14, not 12, so 14+3+1 = 18.
  - 23.14: corrected Step 2 from "1 failed, 2 passed" (claiming the full-text-failure case "already
    pass[es] today") to "2 failed, 1 passed" — the full-text branch has the identical pre-increment defect
    as the abstract branch, so it fails pre-fix too; only `test_a_successful_abstract_fetch_spends_the_budget`
    is a genuine positive control.
  - Part 24 preamble net-ruff claim: see plan/redteam/part_24_applied.md.

## Anchor drifts (Section 5 of the report)

- 23.2: `Files:` line corrected from `resolve_user_name (currently at :654-662)` to `def at :655, edited line
  at :662` (verified: `def resolve_user_name` is at :655, the edited `return user.get(...)` line is at :662).
- 23.4: fixed as part of B2/B3 above (`:615-627`).
- 23.8: fixed as part of M1 above (`def at :108, body to :134`).
- 23.11: `fetch_orcid_grants and fetch_orcid_works (:80-84, :102-106)` corrected to `(:80-87, :102-109)` —
  verified each `try/except`+`return []` block is 8 lines, not 5 (`async with` at :80/:102, `return []` at
  :87/:109).
- 23.13: `Files:` and Step-3 comment both annotated per Minor 9 (the same anchor issue): `imports (:1-11;
  lines 13-14, BIOMEDICAL_AGENCIES, are unchanged and stay below the new constant)` — verified real
  `grants.py:1-11` matches the quoted BEFORE byte-for-byte and `:13-14` is `BIOMEDICAL_AGENCIES`, untouched.
- 23.14: fixed as part of M6 review above (`def at :99, body to :148; branches at :120-138` — the Step-3
  code comment already correctly said `:120-138`; only the top `Files:` line's `:99-143` needed correcting).
- 23.15: `Files:` line corrected from `:229-249, specifically :237-238` to `:225-252, specifically :237-244`;
  Step-3 comment corrected from `(:237-249)` to `(:237-244)` — verified against the real file (`bot_token =
  token_for_agent_row(agent)` at :237 through `agent.delegate_slack_ids = current_ids` at :244, `except
  Exception as exc:` at :245, the surrounding `try:` block runs :225-252).
- 24.1, 24.3, 24.4: applied in part_24.md — see plan/redteam/part_24_applied.md.
- 23.3, 24.2, 24.3 (Files line), 24.4: report says these anchors are already exact — left untouched (no
  anchor fix needed; 24.3's *cap* content fix is separate, see M3 in part_24.md).

## Minors

- Minor 4 (23.7 Step-1 block deletes two sibling tests visually): applied above alongside M1, since it is
  the same `TestAnnouncementOnly` anchor family — changed the anchor to
  `TestAnnouncementOnly.test_positive_cases (:35-44)` and rewrote Step 1 as a BEFORE/AFTER of just that
  method's parametrize list, explicitly noting `test_negative_cases` (:46-61) and
  `test_mixed_announcement_with_substance_allowed` (:63-70) are untouched.
- Minor 9 (23.13 BIOMEDICAL_AGENCIES not marked retained): applied above alongside the 23.13 anchor fix.
- Minor 10 (23.15 needs `createdb copi_x23` first): applied — added an idempotent `createdb ... || true`
  line to Step 2's command (23.4's own copy of this fix is a hard prerequisite there since B3 rewrote that
  task's test location entirely; 23.15's DB will already exist by the time it runs in sequence, but the
  command is now correct standalone too).
- Minors 1, 2, 3 (23.6 IGNORECASE case-leak into cache keys / second call site `funding_rules.py:190,218` /
  wrong cited caller `tools.py`): **skipped** — 23.6 is not in this pass's assigned blocker/major list and
  these three require touching `foa_cache.py`/`funding_rules.py` behaviour or Interfaces text beyond an
  anchor fix (a redesign of `extract_foa_number`'s case handling, or documenting a second call site), which
  Rule D says to skip and log rather than force in under this pass. Flagged for a follow-up pass on Task
  23.6.
- Minor 5 (23.14 leaves `_execute_retrieve_abstract`/`_execute_retrieve_full_text` unreachable from `src/`):
  **skipped** — cosmetic/architectural observation, not a text/anchor fix; the task already documents this
  shape is intentional (mirrors 23.5's helper-removal pattern) and the report itself calls it "defensible".
  Follow-up candidate, not a defect.
- Minor 6 (23.12 docstring update is prose-only): **skipped** — 23.12 is outside this pass's assigned scope
  (not named in the blockers/majors list); flagged for a follow-up pass.
- Minor 7 (23.12 unit assertion parked in the contract tier): **skipped**, same reason as Minor 6.
- Minor 8 (23.11/23.12 live-tier neighbour omitted from Step 5): **skipped** for 23.12 (out of scope this
  pass); the 23.11 half is a Step-5 addition, not an anchor fix, and 23.11 is marked OK — left untouched per
  "do not touch OK tasks except anchors".
- Minor 11 (24.1/24.2 bare `except IntegrityError` masks unrelated constraint failures): **skipped** — both
  24.1 and 24.2 are OK-verdict/needs-fix-for-other-reasons tasks; this is a "worth a sentence either way"
  hardening suggestion, not a correctness defect, and changing the exception handling shape would be a
  substantive behaviour change beyond a text/anchor fix. Follow-up candidate.
- Minor 12 (24.2 `from None` throws away the cause): **skipped** — `from None` is already `B904`-clean as
  written; swapping to `from exc` is a style preference with no functional or lint effect, not a defect.
- Minor 13 (24.1/23.4 "ERROR not FAILED" wording): applied to 23.4 as part of B3's rewrite (the corrected
  Step 2 text no longer characterizes the failure mode incorrectly). 24.1's copy: see part_24.md.
- Minor 14 (24.3 tests inherit a module-level `integration` mark): applied in part_24.md alongside M5.
- Minor 15 (missing `_reply_to_thread` reconciliation row): **skipped** — this is a correction to
  `plan/COORD_A.md`'s cross-part reconciliation table, not to `parts/part_23.md`; out of the fixer's edit
  set for this pass (same reasoning as M2).
- Minor 16 (23.3 annotation drift `list[str | int]` vs `-> list[str]`): **skipped** — cosmetic, no mypy gate
  in `ci.sh` per the report itself; not worth the redesign of widening the return type or adding `str(item)`
  coercion this late in a blocker/major-focused pass. Follow-up candidate.
- Minor 17 (24.3 "default 60s" comment is dead / cap is 30s): applied in part_24.md alongside M3.

## Tests re-run

- `.venv-test/bin/python -m ruff check --select E,F,I,UP,B --ignore E501 src/agent/retry_after.py
  tests/unit/test_retry_after.py` (scratch mirror with a real `pyproject.toml` copy and matching `src/`
  package layout, since ruff's isort first-party detection needs the on-disk package to exist) →
  **All checks passed!** (0 findings; the pre-fix `timezone.utc` version had reproduced B1's 4 UP017 hits
  when run the same way).
- `.venv-test/bin/python -m ruff check --select E,F,I,UP,B --ignore E501 src/agent/grantbot.py` on a full
  scratch copy of the real file with the Task 23.4 AFTER block applied by exact-string replacement (matched
  on the first try, confirming the `:615-627` anchor) → **9 findings, identical set (all pre-existing E402
  top-of-file import-order findings) to the unpatched file** — the patch introduces zero new findings (it
  only adds a local `from ... import` inside a function body, not a module-level import).
- `.venv-test/bin/python -m ruff check --select E,F,I,UP,B --ignore E501
  tests/integration/test_grantbot_token_routing.py` (scratch mirror with stub `src/models/__init__.py`,
  `src/services/__init__.py`, `tests/integration/__init__.py` so ruff's first-party import-sort detection
  matches the real repo) → **All checks passed!**
- Full copy of `src/` + `tests/` (real tree, DB-free): `.venv-test/bin/python -m pytest
  tests/unit/test_funding_rules.py -q -p no:cacheprovider` pre-patch → **32 passed** (confirms the report's
  baseline). Patched `_ACK_SUBSTANTIVE_WORD_COUNT = 10` + the length guard, added M1's two-row parametrized
  test: `-k substantive_reply_starting_with_an_ack_word` → **2 passed**; full file → **34 passed** (32 + 2,
  in isolation from Tasks 23.6/23.7's own additions, which this changelog does not independently re-measure).
- `tests/unit/test_grantbot_selection.py` (real pre-fix `grantbot.py`, all 4 tests) → **3 failed, 1 passed**
  (only `test_a_well_formed_selection_still_works` passes pre-fix). Same file with the corrected AFTER block
  applied → **4 passed**. `tests/unit/test_grantbot_lead_time.py` (neighbour) → **8 passed**, unaffected.
- `tests/unit/test_foa_pattern.py` (new module + test, applied standalone) → **18 passed** (14 parametrized
  family/case rows + 3 NSF-out-of-scope + 1 no-match).
- `tests/unit/test_tools_budget.py` (real pre-fix `tools.py`, all 3 tests) → **2 failed, 1 passed** (only
  `test_a_successful_abstract_fetch_spends_the_budget` passes pre-fix; the full-text-failure case fails too).
