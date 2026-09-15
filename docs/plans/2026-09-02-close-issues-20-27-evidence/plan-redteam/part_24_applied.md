# Changelog — red-team corrections applied to plan/parts/part_24.md

Source report: plan/redteam/part_23_24.md. Order: blockers (none affect Part 24 directly), majors, then
minors/anchors. Part 24 had zero blockers and zero OK-task content changes; 24.2 needed no edits (its only
listed issue, M2, is a correction to plan/COORD_A.md's reconciliation table, not to this file — out of the
fixer's edit set, see part_23_applied.md's M2 entry for the full reasoning).

## Majors

- M3 (24.3): Made the 30s Retry-After cap a keyword parameter on `create_app` —
  `retry_after_cap: float = _MAX_MANIFEST_RETRY_AFTER` — instead of a hardcoded module constant, so the web
  path (`create_app_async`, unchanged signature, still capped by default) is unaffected while
  `scripts/provision_slack_bots.py`'s bulk host script can opt into `retry_after_cap=900.0` and keep waiting
  out Slack's real `Retry-After` instead of burning through all 5 retries in under 3 minutes. Added a
  Coordinator note explaining the one-line `scripts/provision_slack_bots.py:442-445` companion edit as a
  narrow, explicitly-scoped exception to `scripts/*` otherwise being Part 27's file (mirrors the plan's
  existing precedent for Part 23's narrow `simulation.py` exception). Updated the Interfaces/Produces text,
  the Files list (added the script with its ownership-exception note), and the Coverage matrix's C2-3 row.
- M5 (24.3): Added four tests the report identified as missing: `test_every_blocking_entry_point_has_an_async_twin`
  (mirrors `tests/unit/test_slack_web.py::test_every_sync_entry_point_has_an_async_wrapper`, asserting
  coroutine-ness since `slack_provisioning` has no `__all__`) plus one thread-identity test each for
  `exchange_code_async`, `lookup_team_id_async`, and `rotate_config_token_async` — the three single-shot
  twins that previously had no test proving they actually run via `asyncio.to_thread` (Task 24.4's tests
  monkeypatch all three out entirely, so a twin that dropped `asyncio.to_thread` would ship green). Updated
  the top-of-file import-list comment (`import inspect`, `from src.services import slack_provisioning`,
  `create_app_async` added to the explicit import), Step 2's `-k` filter, and Step 4/5's expected counts.
- M6 (Part 24 preamble net-ruff claim): Corrected the preamble's "Task 24.2 actually reduces net findings by
  fixing a latent B904" to state the measured reality — 24.2 is net **0** on `agent_page.py` (43 findings
  before and after; `from None` avoids a 4th `B904`, it doesn't remove one of the 3 pre-existing ones).
  24.3 *is* net -1 on `slack_provisioning.py` (1 `B007` before, 0 after), confirmed correct. Also corrected
  the cross-part framing: the red-team measured the two parts *together* at net +1 (254→255) only because
  Part 23's Task 23.1 (pre-fix) tripped `UP017`; since that blocker (B1) is now fixed in Part 23's own
  changelog, the combined `src/` ratchet effect across both parts is now net **-1** (Part 23: 0, Part 24:
  0 + -1), not +1.

## Minors / anchors

- 24.1: `Files:` line's guard/write block description refined from `:516-534` (unchanged, still correct as
  the overall span) to the sub-structure the report's anchor table gives: `SELECT :516-519, if/else
  :521-533, commit :534` — verified against the real tree (`result = await db.execute` at :516,
  `existing = result.scalar_one_or_none()` at :519, `if existing:` at :521, `else:` at :525, `await
  db.commit()` at :534).
- Minor 13 (24.1's "pytest reports this as an ERROR" wording): corrected to **FAILED** — reproduced directly
  (see Tests re-run): an unhandled exception raised from inside a test body is FAILED, not ERROR; ERROR is
  reserved for collection/fixture-setup exceptions.
- Minor 14 (24.3's new tests inherit a module-level `integration` mark): applied — deleted
  `tests/unit/test_slack_provisioning.py`'s module-level `pytestmark = pytest.mark.integration` and put
  `@pytest.mark.integration` directly on the four tests that take `db_session`
  (`test_rotation_persists_the_whole_triple`, `test_a_cached_token_is_reused_and_does_not_rotate`,
  `test_an_expiring_token_is_rotated_before_it_dies`, `test_a_valid_cached_token_is_returned_untouched`).
  Simplified Step 5's neighbour command from name-based `-k "not test_rotation_persists and not ..."`
  deselection to `-m "not integration"`, re-measured at `14 passed, 4 deselected`.
- Minor 17 (24.3's dead "default 60s" comment): no text existed in the plan claiming 60s "survives" (the
  `default=60.0` argument was simply unexplained); noting it here as **resolved as a side effect of M3** —
  with `retry_after_cap` now parameterized, `default=60.0` is genuinely reachable and meaningful for the
  host script's `retry_after_cap=900.0` override (`parse_retry_after(None, default=60.0, cap=900.0)` returns
  60.0), it is only dead for the web path's `cap=30.0` default. No further plan-text change needed.
- Minor 15 (missing `_reply_to_thread` reconciliation row): **skipped**, same reasoning as M2 — the fix
  targets `plan/COORD_A.md`, not `parts/part_24.md`.
- Minors 11/12 (24.1/24.2 bare `except IntegrityError` / `from None` vs `from exc`): **skipped** — logged in
  part_23_applied.md's Minors section (both are 24.x items but are stylistic/hardening suggestions, not
  defects; the report itself frames them as "worth a sentence either way").
- Section-5 anchor table rows for 24.2, 24.3 (`create_app` :77-152, loop :124-152), 24.4: all marked "exact"
  by the report — verified independently (see Tests re-run) and left untouched; no anchor fix needed.

## Tests re-run

- `tests/unit/test_concurrent_write_guards.py::test_waitlist_submit_survives_a_lost_race_on_email` — full
  copy of the real `src/routers/public.py`. Pre-fix: **FAILED** (not ERROR — confirms Minor 13), exact
  `IntegrityError` propagation from `public.py:534`. Post-fix (AFTER block applied by exact-string
  replacement, matched first try): **1 passed**.
- `tests/unit/test_slack_provisioning.py` — full copy of the real `src/services/slack_provisioning.py` +
  `src/agent/retry_after.py` (Part 23's corrected version). Pre-fix: whole module fails to **collect**
  (`ImportError: cannot import name 'create_app_async'`), confirming the plan's Step 2 claim; the two
  isolated sync-side assertions reproduce exactly: `slept [900] instead of capping at 30.0` and
  `slept [5] on the final iteration before raising`. Post-fix (signature + retry-loop + async-twins patch
  applied by exact-string replacement, matched first try): targeted `-k` subset → **7 passed** (3 original +
  4 new M5 tests); full non-integration run (`-m "not integration"`, after Minor 14's marker fix) →
  **14 passed, 4 deselected**.
- `.venv-test/bin/python -m ruff check --select E,F,I,UP,B --ignore E501 src/services/slack_provisioning.py
  tests/unit/test_slack_provisioning.py` (post-fix) → **All checks passed!** Baseline (unpatched)
  `ruff check src/services/slack_provisioning.py` → **1 finding** (`B007`, unused `attempt`); patched → **0**
  — confirms Task 24.3's net **-1** claim exactly.
- `src/routers/agent_page.py`: `ruff check` on the real file → **43 findings** (3 `B904`) before Task 24.2's
  patch; same file with the AFTER block applied by exact-string replacement (matched first try) → **43
  findings** (3 `B904`, unchanged) — confirms the corrected preamble's net-**0** claim for 24.2 exactly.
