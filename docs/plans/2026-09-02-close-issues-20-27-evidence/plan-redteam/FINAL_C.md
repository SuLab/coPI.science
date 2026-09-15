# FINAL_C — Section C confirmation pass (Parts 23, 24, 25, 26, 27)

Scope: `MASTER_PLAN.md:12994-15762` (23), `15763-17237` (24), `17238-18720` (25), `18721-20296` (26),
`20297-22636` (27), read against lines `1-79` (constraints + reconciliation) and `22987-23222` (Part R).
Audits verified: `redteam/part_23_24.md` (3 BLOCKER / 6 MAJOR / 17 MINOR) and
`redteam/part_25_26_27.md` (14 BLOCKER / 13 MAJOR / 21 MINOR).
Repo `/home/a/scripps/coPI.science` read-only throughout (`git status --short` empty at start and end);
all measurements below run in `…/scratchpad/finalC` (patched) vs `…/scratchpad/finalC_pre` (pristine)
with `/home/a/scripps/coPI.science/.venv-test/bin/python`.

**Note on inputs:** `redteam/part_25_applied.md`, `part_26_applied.md` and `part_27_applied.md` do **not
exist** (only `part_20/21/22/23/24_applied.md` do). `parts/part_25.md` (08:28), `part_26.md` (08:30) and
`part_27.md` (08:31) were all rewritten after the audit (Sep 2 19:56), and every finding below was verified
directly against the assembled plan text + the real code, so the missing changelogs cost nothing — but no
map existed for those three parts.

---

## 1. Verdict table — part_23_24.md

| id | task | verdict | evidence (MASTER_PLAN.md line) |
|---|---|---|---|
| B1 | 23.1 `UP017`×4 breaks `ci.sh`'s zero-findings `tests/` lint | **APPLIED** | `13176` `from datetime import UTC, datetime`; `13097` test uses `from datetime import UTC, ...`; new Step 5 lint gate `13280-13286`. **Measured**: patched tree `ruff check src` = **254** (= baseline, not 256) and `ruff check tests` = `All checks passed!`; `ruff check --select E,F,I,UP,B --ignore E501 src/agent/retry_after.py tests/unit/test_retry_after.py` = 0. |
| B2 | 23.4 remediation cannot restore GrantBot | **APPLIED** | `13741-13757` AFTER = `await get_agent_bot_token(session, "grantbot") or getattr(settings, "slack_bot_token_grantbot", "")` + `is_valid_token` + ERROR-and-refuse; corrected Deploy note `13812-13818` (DB → next daily run, no restart; `.env` → `up -d --force-recreate grantbot`, `get_settings()` is `lru_cache`d). Coordinator note `13570-13576` names D10. Verified in code: `slack_tokens.get_agent_bot_token` validates with `is_valid_token` before returning, so a placeholder DB row correctly falls through to the `.env` field; anchor `:615-627` is byte-exact and the `asyncio.to_thread(_ensure_channel_membership, …)` block at `:628+` stays inside the new `if candidate:`. |
| B3 | 23.4 test can neither fail nor pass (`live_api` skip) | **APPLIED** (better than prescribed) | test moved to a **new** `tests/integration/test_grantbot_token_routing.py` with `pytestmark = pytest.mark.integration` (`13611-13657`), so no `LIVE_API_TESTS=1` is needed at all; `createdb -U copi copi_x23` added (`13729`); `-m "integration and live_api"` gone. |
| M1 | 23.8 does not close COR-28b's own 11-word example | **APPLIED** | `_ACK_SUBSTANTIVE_WORD_COUNT = 10` (`14311`), rationale `14298-14310`, the literal `"Agreed, we can send the plasmids and the mice next week."` row added (`14265`), Open Decision rewritten (`14351-14353`). **Measured**: pre 2 failed / 32 passed → post 34 passed. |
| M2 | reconciliation item 2 misdescribes 21.13, breaks 24.2's test | **APPLIED** | item 2 rewritten at `62` (retire lands in the "Already reviewed" branch or after the guarded commit, never un-committed inside the try). 21.13's own text (`9497-9523`) pre-captures `current_user_id`/`agent_registry_id` **before** the try and keeps the success-path execute order agent → thread_decision → existing-review → `record_engagement`, so 24.2's `raise_at=4` still points at the autoflush site; it also patches `_ReviewRaceSession.execute` + `_FakeResult.scalars()`. |
| M3 | 24.3's 30 s cap silently applies to the bulk host script | **APPLIED** | `retry_after_cap: float = _MAX_MANIFEST_RETRY_AFTER` in the signature (`16587`), `cap=retry_after_cap` (`16675`), and `scripts/provision_slack_bots.py:442-445` gains `retry_after_cap=900.0` (`16602`). Anchor verified: `create_app(` is at `provision_slack_bots.py:442`. |
| M4 | 23.3 costs a whole day of funding posts; comment says otherwise | **APPLIED** | corrected comment `13508-13518` (names `_mark_run_complete()` / `grantbot.py:777` / the ~64-attempts-a-day alternative); coverage-matrix COR-26b′ row now says 23.3 *removes* the trigger (`15687`). |
| M5 | three of four async twins untested | **APPLIED** | `test_every_blocking_entry_point_has_an_async_twin` (`16421`) and `test_exchange_code_async_runs_off_the_event_loop` (`16432`) both present; minor 14's marker fix applied — module-level `pytestmark` deleted, `@pytest.mark.integration` moved onto the four `db_session` tests, which I confirmed are exactly `:175/:193/:213/:228`. |
| M6 | several "measured" outputs are not real | **PARTIAL** | Fixed: 23.3 Step 2 → `3 failed, 1 passed` with the corrected control note (`13476-13480`); 23.14 Step 2 → `2 failed, 1 passed` (`15439-15443`); 23.6 Step 4 → `18 passed` (`14110`); 23.1 Step 4 → `67 passed` (`13260`) — I measured **67** exactly; Part 24 preamble net-ruff paragraph rewritten (`15795-15804`) and I measured net 0 for 23.1. **NOT fixed: 23.2 Step 5 still says "64 → 66 passed"** (`13371`). Measured after 23.1 lands first (phase 2 before phase 5): the file is 67, and 23.2 takes it to **69**. See NEW-2. |

MINORs applied: 4 (23.7 anchor → `TestAnnouncementOnly.test_positive_cases (:35-44)`, `14132-14134`),
6 (23.12 docstring before/after, `15063-15065`), 9 (23.13 `BIOMEDICAL_AGENCIES` retained, `15205`/`15245`),
10 (`createdb copi_x23`, `13729`/`15620`), 13 (FAILED-not-ERROR, `15987-15991`), 14 (marker fix, `16314-16320`).
MINORs still open: 1/2/3 (23.6 — see NEW-5), 5, 7, 8 (23.12 — see NEW-6), 11, 12 (24.2's `from None` is now
an explicitly argued decision, `16247-16256` — accept), 15, 16, 17.

## 2. Verdict table — part_25_26_27.md

| id | task | verdict | evidence |
|---|---|---|---|
| B1 | 25.4 test passes on unfixed code | **APPLIED** | `_BADGE_TABLES` / `_RecordingSession` / `_badge_queries` test (`18152-18215`). Confirmed against real `src/main.py:31-93`: `dispatch` issues the `agent_registry` + `agent_delegates` SELECTs on *every* path once a session cookie carries `user_id`, so the recording fake makes the test red pre-fix. |
| B2 | 25.4 × 27.2 contradict on `/api/health` | **APPLIED** | binding cross-part note in 25.4 (`18120-18124`) and in 27.2 (`20612-20620`); reconciliation item 16 at `76`; 27.2 Step 5 re-runs `test_agent_badge_middleware.py` (`20745-20748`). |
| B3 | 25.3 Step 4/5 "expect PASS" is false | **APPLIED** | "Deliberately NOT done" gone; `18054-18060` + `18447-18456` hand `postflight.EXPECTED_INDEXES` to M.1 and drop the pin files from `git add`; Steps 1/2 replaced with the import/grep checks. |
| B4 | 26.2 replacement still matches the forbidden substring | **APPLIED** | new row `18896` = `*Currently unused by any code — …email_inbound.py:449-475…*`; test asserts `"…| LLM prompt for classifying" not in spec` and `"email_inbound.py" in spec`. BEFORE line verified byte-exact at `specs/email-proposal-review.md:266`. |
| B5 | 26.3 re-introduces `PILOT_LABS` | **APPLIED** | `18964` `there is no hardcoded roster list in \`src/agent/simulation.py\``. |
| B6 | 26.4 re-introduces `/api/admin/impersonate` | **APPLIED** | `19062` `originally described an \`/api/\`-prefixed design`. |
| B7 | 26.4 "email in scope" lands inside Out-of-Scope | **APPLIED** | `19076-19082`: the three bullets stay under `## What's Out of Scope`, the email prose is a new `## What Email Actually Does (in scope, built)` section, so the test's `split("##",1)[0]` slice is email-free. |
| B8 | 26.5 two-line `**Pilot:**` defeats its own test | **APPLIED** | single-line replacement at `19169` carrying `/admin/agents`. |
| B9 | 27.1 test red before *and* after | **APPLIED** | `data/agent_roster.json` removed from `MUST_NOT_EXCLUDE` + the reason note (`20465-20470`); `zip(…, strict=False)` (`20438`). **Measured**: pre 15/16 `MUST_EXCLUDE` fail and 0 `MUST_NOT_EXCLUDE` fail; post 0 and 0. |
| B10 | 27.3 `migrate` missing from the override → full outage | **APPLIED** | override entry (`20898-20900`), `assert prod <= override` test (`20852-20855`), and notes (a) `--no-deps`, (b) `--profile agent`, (c) reboot path (`20940-20946`). **Measured** on a scratch copy: prod-only `migrate` → the `<=` test FAILS (`no json-file logging override for ['migrate']`); with the override entry → 3 passed. Also confirmed the real override enumerates exactly 7 services. |
| B11 | 27.4 duplicate TOML table | **APPLIED** | note `20963-20968` + `scripts` inside the existing table (`21120-21132`). **Measured**: `tomllib.loads` clean, 3 tests red → green, `ruff` clean (B034 gone via `maxsplit=1`). |
| B12 | 27.5 staleness diff can never match | **APPLIED** | `--no-header` on both sides + `diff <(grep -v '^#' …)` (`21252-21266`), `import piptools` prerequisite (`21230-21234`), `LOCKCHECK=none` (M9, `21243-21245`, documented at `21322-21326`). |
| B13 | 27.7 runtime comment trips its own `gcc` assert | **APPLIED** | runtime comment (`21519-21523`) contains neither `gcc` nor `libpq-dev`; `text.rindex("FROM python:3.11-slim")` lands on the `AS runtime` line. |
| B14 | 27.12 regex cannot match the real route | **APPLIED** | `location ~ ^/admin/agents/[^/]+/slack/provision` (`22112`) + `test_provisioning_location_regex_matches_the_real_route_path` (`22067-22075`) + the "regex correctness matters" paragraph (`22047-22056`). Confirmed `src/routers/admin.py:964` = `@router.post("/agents/{agent_id}/slack/provision")`. |
| M1 | 25.3's red/green cycle voided | **APPLIED** (same text as B3) |
| M2 | 0027's loop form voids the drift guard | **APPLIED** | 20 explicit `op.create_index("ix_…", …)` literals (`17708-17745`) + the sabotage rationale (`17684-17691`); `downgrade()` keeps the loop. |
| M3 | lint ceiling consumed by B904 | **APPLIED** | `from exc` in 25.2 ×2 (`17556`, `17579`), 26.10 (`19850`) with the ceiling note (`19860-19862`); 25.6's body reworked. |
| M4 | new test files break the `tests/` lint step | **APPLIED** | `maxsplit=1` (27.4) and `strict=False` (27.1); **measured** `ruff check tests` = `All checks passed!` on the patched tree, and 0 findings on each new file individually. |
| M5 | 25.2 Step 2 expectation wrong | **APPLIED** | `17529-17534` now says ERROR + `raise_app_exceptions=True`. |
| M6 | 27.12 `nginx -t` recipe fails | **APPLIED** | full `--add-host` recipe + dummy certs + the expected `ssl_stapling` warnings (`22140-22166`). |
| M7 | 27.13 mem_limits exceed host RAM | **APPLIED** | sum recomputed to **2528 MiB** (`22195-22203`), `test_the_copi_python_mem_limits_sum_comfortably_under_the_host_total` with `<= 3072` (`22224-22233`), measure-first `docker stats`/`free -m` step (`22243-22249`), `cpus`-is-not-a-budget note (`22268-22272`). |
| M8 | 27.14 ships a literal placeholder, no mypy guard | **APPLIED** | `import mypy` prerequisite (`22440-22444`); measure-and-hard-code step (`22421-22433`); shipped literal `842`; plus a **new** guard test `test_mypy_max_is_a_real_integer_not_a_placeholder` (`22374-22386`). |
| M9 | 27.5 makes `ci.sh` network-dependent | **APPLIED** | `LOCKCHECK` escape hatch + "Overridable env" doc. |
| M10 | reconciliation item 6's CLAUDE.md edit had no owner | **APPLIED** | 27.1 Files (`20351-20352`), Step 3 note (`20517-20522`), the `=== Turn 1: <agent> ===` fix (`20524-20527`), `git add CLAUDE.md` (`20539`). |
| M11 | R.7 expects a health body 27.2 does not produce | **APPLIED** | R.7 now says `Expect exactly b'{"status":"ok"}' … the 200 body is unchanged (no "db" key)` (`23138-23141`). |
| M12 | 26.6's BEFORE cannot match (mojibake) | **APPLIED** | content-anchored instruction + `hexdump` evidence (`19250-19256`). Confirmed `specs/tech-stack.md:213` starts `ef bf bd ef bf bd ef bf bd`. |
| M13 | 25.5/25.6 name `main()` not `_run_simulation()` | **APPLIED** | `_run_simulation()` used throughout (`18285`, `18297-18298`, `18429`, `18469`, `18576`, `18603`). |

MINORs applied: 3 (`from tests import factories` really is at `:30` — the audit was wrong, the plan is
right), 5, 10, 11, 12 (`:47-70`/`:72-77`, confirmed), 18 (`test_health_route_unit.py`), 19, 21.
MINORs still open: 1, 2, 4, 6, 7, 8, 9, 13, 14, 15, 16, 17, 20.

## 3. Cross-cutting checks

**Item 7 — AsyncSession `MissingGreenlet`/`PendingRollbackError`.** Only five rollback sites exist in Parts
23-27: `15998` (24.1), `16225` (24.2), `17556` + `17579` (25.2), `19846` (26.10). **All five are clean, no fix
needed:**
- 24.1: after `await db.rollback()` the only reads are `email_clean` (a local `str`) and a static template
  context (`{"request": request, "waitlist_success": True}`) — verified against `src/routers/public.py:534-541`,
  which touches no ORM instance. `get_db`'s trailing `await session.commit()` on a rolled-back session is a
  no-op begin/commit.
- 24.2: `await db.rollback()` → `raise HTTPException(400, "Already reviewed") from None`, no attribute read in
  between; the redirect that would read `agent_id` (a path-param `str` anyway) is unreachable on that path.
  When 21.13 lands on top it adds two post-rollback `db.execute`s but has already pre-captured
  `current_user_id`/`agent_registry_id` into locals before the `try` (`9498-9506`), and the plan says so
  explicitly with the reproduction.
- 25.2 (admin): `name = user.name` is captured **before** the `try` (unchanged from the pre-fix code);
  after rollback it raises. 25.2 (profile): raises immediately after rollback.
- 26.10: raises immediately after rollback.
`src/database.py:36-43` confirmed: `async_sessionmaker(get_engine(), class_=AsyncSession,
expire_on_commit=False)` — autoflush on, so 24.2's central claim holds. (Plan cites `:39-43`; the call opens
at `:38`. Cosmetic.)

**Item 8 — no prompt changes.** ✅ Clean for 23-27. Every `prompts/` mention in the section is either the D33
scope note (`18732-18734`), 26.2's spec-row text *about* `prompts/email-reply-classify.md` (no file touched —
"both stay exactly as they are on disk", `18853-18856`), a `.dockerignore` `MUST_NOT_EXCLUDE` entry
(`20478`), or 27.13's `./prompts:/app/prompts` compose mount. `grep` for
`SELECTION_PROMPT|_build_selection|profile_pipeline.py:3|classify prompt|system_prompt|user_prompt` over
`12994-22636` → **0 hits**. **26.2 touches no file under `prompts/` — confirmed (D33).**

**Item 9 — Part R vs the corrected Part 27.** R.3's `build --no-cache app worker grantbot migrate`
(`23061`) is satisfied: 27.3 defines `migrate` with `build: context: .` (`20866-20868`). R.3 additionally
verifies the override with `docker compose $C config | grep -B2 -A3 -E '^  migrate:' | grep -c awslogs`
→ must print 0 (`23065`) — a good independent check on B10. R.7's `--no-deps` escape hatch is present
(`23156`). R.7b is consistent with 27.13 (`certbot` recreate + `docker inspect … Memory NanoCpus` → `0 0`
for the deliberately-unlimited postgres, per D24); R.7/R.8 recreate app/worker/grantbot/nginx, so every
other limit does apply. R.7's health expectation matches 27.2 (M11).

**Item 4 (my parts).** Task numbering intact: 23.1-23.15 (15), 24.1-24.4, 25.1-25.6, 26.1-26.13,
27.1-27.14 — all present, none duplicated, none missing. Coverage matrices present at `15678` (23),
`17156` (24), `18619` (25), `20200` (26), `22540` (27). Zero `HEAD_REVISION` literals in `12994-22636`.
No `TBD` / `similar to`; the only `TODO`/`<N>` hits are 26.13 (whose subject *is* a TODO-looking comment)
and 27.14 (which quotes `<N>` as the anti-pattern it guards against, and ships `842`).

**Item 5 — six end-to-end spot-checks** (diff applied to a scratch copy by exact-string replacement, then
the DB-free test run in both trees). Every BEFORE block matched byte-for-byte on the first attempt.

| # | task | size | fails before | passes after |
|---|---|---|---|---|
| 1 | 23.1 (`retry_after.py` + `slack_client.py` ×3) | large | `ModuleNotFoundError: src.agent.retry_after` + `3 failed, 64 deselected` (`ValueError: invalid literal for int()`, `[99999999] != [30.0]`, `[-5] != [0.0]`) | `77 passed` (10 + 67); `ruff` 0 on both new files; `ruff check src` 254, `ruff check tests` clean |
| 2 | 23.2 (`resolve_user_name`) | small | `1 failed, 1 passed` (`'Real Name' == 'Display Name'`; the fallback control stays green) | `69 passed` (whole file) |
| 3 | 23.8 (`_ACK_SUBSTANTIVE_WORD_COUNT`) | medium | `2 failed, 32 passed` — incl. COR-28b's literal 11-word sentence | `34 passed` |
| 4 | 27.1 (`.dockerignore`) | medium | 15/16 `MUST_EXCLUDE` unmatched; `MUST_NOT_EXCLUDE` clean | 0 / 0 |
| 5 | 27.4 (`pyproject.toml` caps + `scripts` extra) | medium | `3 failed` | `3 passed`; `tomllib` parses; `ruff` clean |
| 6 | 27.3 (`migrate` + override) | large | `2 failed, 1 passed` (`KeyError: 'migrate'`) | `3 passed`; and with the override entry removed the `<=` test correctly goes red |

Anchors additionally re-verified against the live tree: `slack_client.py` imports / `MAX_RETRIES` /
`_call_with_retry` / `resolve_user_name`; `grantbot.py:615-627` and `_run_grantbot_with_session(session, …)`
at `:489-495`; `funding_rules.py` `_ACK_RE` + `is_acknowledgment_only_funding_reply`; `pyproject.toml:11-29`,
`[project.optional-dependencies]:31`, `dev:32-54` (11 entries at `:33-53`); `Dockerfile:11-14`;
`docker-compose.override.yml`'s 7 services; `admin.py:213` (`admin_delete_user`) and `:964`
(`/agents/{agent_id}/slack/provision`); `admin_provisioning.py` imports `:21-25`, `_config_token:69`,
`start_provisioning:124`, `complete_provisioning:190`; `provision_slack_bots.py:442-445`;
`test_slack_provisioning.py`'s four `db_session` tests at `:175/:193/:213/:228`;
`backfill_agents.py:47`/`:72`; `specs/tech-stack.md:213` (3× U+FFFD);
`specs/email-proposal-review.md:266`; `slack_tokens.py:20-37`/`:54-64`.

---

## 4. New findings

### NEW-1 (MAJOR) — Part 26's four in-container pytest commands never pass `TEST_DATABASE_URL` into the container
`MASTER_PLAN.md:19653`, `19745`, `19808`, `19862` all read:

```
TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a26 docker compose exec -T app python -m pytest …
```

The assignment applies to the **`docker compose` client on the host**, not to the process inside the
container, so `tests/conftest.py` sees no `TEST_DATABASE_URL` and falls back to spinning an ephemeral
Postgres via testcontainers — which the `app` container cannot do (no Docker socket). Per CLAUDE.md that is
469 erroring tests, measured. Every other container command in Parts 23/24/25 uses the correct `-e` form
(`13730-13732`, `15618-15622`, `16011-16012`, `16260-16261`, `17521-17525`, `17555`), so this is Part 26
alone. Replace all four with (per reconciliation item 14, the DEV file, and `unset COMPOSE_FILE` first):

```
docker compose -f docker-compose.yml exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a26 \
  app python -m pytest tests/integration/test_agent_page.py::<name> -v
```

(26.9's `createdb -U copi copi_a26` note at `19654` is already correct and covers 26.10 too.)

### NEW-2 (MINOR) — 23.2 Step 5's neighbour count is stale (residual M6)
`13371`: "full `tests/unit/test_slack_client_contract.py` (64 → 66 passed with this task's 2 additions)".
23.1 lands in **phase 2** and 23.2 in **phase 5**, so by the time 23.2 runs the file is at 67. Measured:
**69 passed** after 23.2. Replace with `(67 → 69 passed with this task's 2 additions; 64 is the pre-23.1
baseline)`.

### NEW-3 (MINOR) — 27.4 mis-states the extent of the existing TOML table
`20963`: "`pyproject.toml` already has exactly one `[project.optional-dependencies]` table (`:31-80`,
containing `dev`)". Real extent is `:31-54` (`dev` at `:32-54`); `:56` is `[project.scripts]`. The rest of
the task (including "`dev`'s existing 11 entries, `:33-53`, and the table's closing `]` at `:54`") is right.

### NEW-4 (MINOR) — R.5 chowns `profiles data` but 27.8's own deploy note also chowns `prompts`
`23122` vs `21660-21661`. Harmless in practice (prompts are read-only and world-readable), but the two
lists should match; add `prompts` to R.5's `chown` or drop it from 27.8's note.

### NEW-5 (MINOR) — 23.6's three unaddressed audit minors are still exactly as the audit found them
`13921-13922` still claims `foa_cache.extract_foa_number` is "consumed by `simulation.py:22` and
`tools.py:153`" — verified `grep -rn extract_foa_number src/ tests/ scripts/` returns only
`foa_cache.py:81`, `simulation.py:22,1119,1217,1258`; `tools.py:153` is
`from src.agent.foa_cache import format_foa_for_prompt`. The task's Files line also still names only
`funding_rules.py:95` while `_FOA_NUMBER_RE` is used at `:121` **and** `:190`
(`summarize_funding_thread`), and there is no `.upper()` normalisation (nor a stated reason) for the new
IGNORECASE match feeding `PostRef.foa_number` → `foa_cache`'s case-sensitive filename key.

### NEW-6 (MINOR) — 23.12 minors 7 and 8 unaddressed
`test_semaphores_are_sized_by_api_key_presence` still lives in `tests/contract/test_pubmed_contract.py`
(inheriting `pytest.mark.contract` for a test that touches no HTTP), and Step 5 (`15181-15186`) still omits
`tests/integration/test_profile_pipeline_live.py:835-870`, whose `_ncbi_get` cost rises to 4 attempts +
3.5 s backoff + pacing per call under 23.12's retry wrapper.

### NEW-7 (MINOR) — 27.13's sum assertion silently mis-reads a gigabyte suffix
`22231`: `int(services[name]["mem_limit"].rstrip("mg"))` turns a future `"1g"` into `1`. Correct today (all
eight values use `m`) but it makes the guard fail open the first time someone writes `1g`. Suggest
`_mib(v) = int(v[:-1]) * (1024 if v.endswith("g") else 1)`.

---

## 5. Counts

- Audit findings verified: **17 BLOCKER → 17 APPLIED**; **19 MAJOR → 18 APPLIED, 1 PARTIAL** (M6, one
  stale count in 23.2 Step 5); **38 MINOR → 14 applied, 24 still open** (all cosmetic/anchor-drift or
  explicitly-argued decisions; `24.2`'s `from None` is now a documented choice).
- New findings: **1 MAJOR** (NEW-1), **6 MINOR** (NEW-2 … NEW-7). **0 new BLOCKER.**
- Spot-checks: **6/6** red-before, green-after, byte-exact anchors.
- Item 7 (MissingGreenlet): **0 exposed tasks** in Parts 23-27 — all five rollback sites verified safe.
- Item 8 (no prompt changes): **clean**; 26.2 touches no `prompts/` file (D33 confirmed).
- Item 9 (Part R ↔ Part 27): **consistent** — `migrate` has `build:`, R.3 verifies the override,
  R.7 has `--no-deps`, R.7b matches 27.13/D24.
- Item 4 (my parts): numbering intact, 5/5 coverage matrices, 0 `HEAD_REVISION`, 0 `TBD`/`similar to`.

## 6. Verdict

**GO for Parts 23-27**, conditional on two text-only edits (neither changes a source diff, a test body, or
a migration):

1. **NEW-1 (MAJOR, must change):** fix the four Part 26 pytest commands at `MASTER_PLAN.md:19653`, `19745`,
   `19808`, `19862` to the `docker compose -f docker-compose.yml exec -T -e TEST_DATABASE_URL=… app` form.
   As written they cannot run, and their failure mode (469 testcontainer errors) looks like a broken suite
   rather than a broken command.
2. **NEW-2 (MINOR, should change):** correct 23.2 Step 5's `64 → 66` to `67 → 69` — it is an executor's
   oracle, and as written a correct run looks like a regression.

NEW-3 … NEW-7 and the 24 open audit MINORs are accept-as-is: none blocks execution, none changes a diff.
