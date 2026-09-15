# Issue #23 — red-team pass on the first agent's report

Tree: `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c, clean. Re-derived every row by fresh grep/read; every
mechanical claim re-run with `.venv-test/bin/python` (scripts in `scratchpad/rt23/probe.py`, `probe2.py`; the second
probe hard-blocks `socket.connect`). Pre-fix code read via `git show <fix>^:<path>` only.

## 1. Table

| id | first-agent verdict | red-team | one-line reason | evidence |
|---|---|---|---|---|
| V7a | FIXED (9dbc9e0) | UPHELD | pre-fix `raise SlackApiError(..., response=exc.response)` after the loop (`git show 9dbc9e0^` :167) → UnboundLocalError; test :146-169 would fail pre-fix on both asserts | `slack_client.py:324-342`; test body read |
| V7b | FIXED (d311170) | UPHELD | pre-fix was a single `conversations_history(limit=limit)` call (d311170^ :216-217); no unpaginated poller survives — `poll_dm_messages` delegates to `poll_channel_messages`; `get_thread_replies`/`get_all_thread_replies`/`get_full_channel_history` all go through `_paginate`; sim cursor advances per message at `simulation.py:2777` (bot) and `:2837` (human) | `slack_client.py:534-535,571-572,602-603,633-634,856`; tests :599-660 |
| V7c | FIXED (d311170) | UPHELD (residual in scripts only) | pre-fix single `conversations_list(types, limit=200)` (d311170^ :619); `grantbot._ensure_channel_membership` now uses paginated `slack_web.list_channel_ids`; only unpaginated listing left is `scripts/wipe_slack.py:231` (`limit=200`, no cursor) — a host script, not a prod process | `slack_client.py:1039-1040`; `slack_web.py:125-177`; `grantbot.py:440-443` |
| V7d | STILL PRESENT (med) | UPHELD (UNVERIFIABLE on API shape) | code unchanged; no offline artefact in repo or slack_sdk shows `users.info` shape; only other consumer `agent_page.py:1378` reads `real_name`/`name` | `slack_client.py:662` |
| V7e | STILL PRESENT | UPHELD + sharpened | reproduced: HTTP-date → `ValueError` escapes; **also `"2.5"` (a legal delta-seconds header is integer, but proxies emit floats) escapes**; `99999999` and `-5` accepted; slack_web's `_call` has the cap/float but `slack_client` does not | probe output §2 |
| C26a | STILL PRESENT | UPHELD | `:344-346` fallback; `{}`/`123`/`null`/`garbage` all → full key list (reproduced) | probe2 |
| C26b | STILL PRESENT | UPHELD | dict/list element → `TypeError: unhashable` at `:538` (reproduced); no isinstance check | probe2 |
| C26b' | STILL PRESENT | UPHELD | `_mark_run_complete()` only after success `:777`; `except Exception` logs; `_should_run_today()` stays True; re-fires every `check_interval` (900 s default; compose passes none) while `now.hour >= 8` UTC | `grantbot.py:767-786`; `docker-compose.prod.yml:116` |
| C26b'' | NOT REPRODUCIBLE | UPHELD | prod runs `scheduler` (compose :116); `except Exception` swallows `TypeError`; only `BaseException` (KeyboardInterrupt/SystemExit/CancelledError out of `asyncio.run`) escapes, and compose `restart: unless-stopped` (:115) would restart even that. The issue is wrong for the prod path; the one-shot `main` (:724-745) would exit non-zero but is not what runs. Refinement: re-fire is bounded to 08:00–23:59 UTC (≤64 attempts/day), and each attempt re-calls the LLM so it can succeed | `grantbot.py:749-786` |
| C26c | STILL PRESENT | UPHELD | 18ba52c only changed INFO→WARNING (diff confirms); engine `_resolve_service_bot_uids` explicitly refuses su-token fallback and `_bot_uid_map` resolves roster first | `grantbot.py:615-625`; `simulation.py:3967-3971,4004-4010` |
| C26d | STILL PRESENT | UPHELD | only refs are in-file `:55,68-71`; `PROFILES_DIR` at `:44` is grantbot-local (other modules define their own) | grep over src/scripts/tests |
| C26e | FIXED (confirmed) | UPHELD | `_claim_foa` ON CONFLICT DO NOTHING + commit, `_release_foa` delete + commit | `grantbot.py:246-275` |
| C27a | STILL PRESENT | UPHELD | all 6 rows reproduce exactly | probe §2 |
| C27b | STILL PRESENT | UPHELD | both `flags & IGNORECASE` False | probe |
| C27c | STILL PRESENT | UPHELD | `extract_foa_number` → None for PAR/PA/PAS/DE-FOA; docstring `:18` lists 2 failing examples | `foa_cache.py:18-21,81-84` |
| C27-new (NSF) | new claim | QUALIFIED | mechanically true that neither regex admits any prefix other than RFA/PAR/PA/NOT/OTA/RFI/DE-FOA — so no NSF number can match — but the actual Grants.gov `number` format for NSF is not verifiable offline (no fixture in repo) | probe: `NSF 25-543`, `25-543`, `PD 24-7275` all False |
| C28a | STILL PRESENT | UPHELD | class codepoints `['0x27','0x27']`; U+2019 forms → False | probe |
| C28b | STILL PRESENT | UPHELD | `"Agreed, we can send the plasmids and the mice next week."` → True | probe |
| C28b' | STILL PRESENT | UPHELD | reset only `:1506`; increment `:1457`; back-off `:1463-1466`; `has_pending_reply=True` at 9 literal sites + 1 variable (`:4242`) = the first agent's "≥10" | grep |
| C28c | STILL PRESENT | UPHELD | `@GRANTBOT`/`@SuBOT` → None | probe |
| C28d | N/A + note | UPHELD | `message_log.py:407` still case-sensitive (18ba52c added a service-bot skip, not IGNORECASE); `simulation.py:2523,2558` same literal | grep |
| C29a | STILL PRESENT | UPHELD | no retry/backoff/tenacity in the three files | grep |
| C29b | STILL PRESENT | UPHELD | `raise_for_status()` `:94` before `sleep(0.12)` `:95` | `pubmed.py:91-96` |
| C29c | STILL PRESENT (qualified) | QUALIFIED | "all callers sequential except Phase 4 gather" is true for the agent/worker (`src/worker/main.py` has no gather/create_task) but omits the **app** process: `auth.py:189` `fetch_orcid_profile` and other request handlers run concurrently per HTTP request, so concurrent logins/onboardings are a second concurrency source through the same process-local `Semaphore(8)` | `auth.py:189`; `pubmed.py:73` |
| C29d | context | UPHELD | `_ncbi_get` :76-90 sets tool/email only | — |
| C30 | STILL PRESENT | UPHELD | debit `:128`/`:137` before await; `except` `:146-148`; `_execute_retrieve_abstract` `:205-207` returns `result["error"]` for the non-raising `{"error":…}` at `pubmed.py:434,439` — new claim reproduced by read | `tools.py:126-148,203-207` |
| V10a | FIXED (02143de, fa143a6) | UPHELD (no regression test) | pre-fix imported `_get_bot_token` from agent_page + raw `WebClient` + bare `except` (02143de^ :234-245); current uses `token_for_agent_row` + `lookup_user_by_email_async` + `logger.warning`. **No test exercises invite.py's sync**: `lookup_user_by_email_async` appears only in `tests/unit/test_slack_web.py`; `tests/integration/test_agent_page.py:819` covers the agent-page delegate link, not the invite path — the issue's "definition of done" is unmet for V10 | `invite.py:232-252` |
| V10b | STILL PRESENT (nit) | UPHELD | `if bot_token:` with no else | `invite.py:237-238` |

New claims by the first agent, each re-verified:
- `slack_web._call` float()+try+30 s cap — **confirmed** (`slack_web.py:49,104-116`).
- `fetch_abstract` non-raising `{"error":…}` charged to budget — **confirmed** (`pubmed.py:434,439`; `tools.py:128,205-207`).
- NCBI callers sequential except Phase 4 — **qualified** (see C29c).
- NSF numbers match neither regex — **qualified** (see C27-new).
- 18ba52c raised log level only; engine refuses su→grantbot mapping — **confirmed**.
- bare JSON string iterates char-by-char → 0 selected silently — **reproduced**: `'"PAR-24-293"'` → `sel='PAR-24-293'`, chosen `[]`, log says "Selected 10 of 2 opportunities".
- `tools.py:122-123` comment contradicts code — **confirmed** (increments for every `retrieve_abstract`).
- "≥10 re-arm sites" — **confirmed** (9 literal + 1 variable).

## 2. Detail for QUALIFIED rows and sharpened items

**V7e (sharpened).** Real `_call_with_retry` with `time.sleep` stubbed:
```
'Wed, 21 Oct 2015 07:28:00 GMT'    -> ESCAPES as ValueError: invalid literal for int() with base 10: 'Wed, 21 Oct 2015 07:28:00 GMT'
'99999999'                         -> SlackApiError (callers catch) sleeps=[99999999, 99999999, 99999999]
'-5'                               -> SlackApiError (callers catch) sleeps=[-5, -5, -5]
'2.5'                              -> ESCAPES as ValueError: invalid literal for int() with base 10: '2.5'
```
The `-5` case: the real `time.sleep(-5)` raises `ValueError` too (the probe's stub hides it), so a negative header is a
third escape route. `tests/unit/test_slack_client_contract.py:110-113` autouse-stubs `time.sleep`, so the suite can never
observe any of these.

**C26b'' (NOT REPRODUCIBLE, upheld with precision).** `grantbot.py:767-786`:
```
    while True:
        now = datetime.now(UTC)
        if _should_run_today() and now.hour >= run_hour:
            try:
                results = asyncio.run(run_grantbot(...))
                _mark_run_complete()
            except Exception as exc:
                logger.error("Daily run failed: %s", exc, exc_info=True)
        ...
        time.sleep(check_interval)
```
- The `TypeError` from `:538` is an `Exception` → caught → loop continues. Nothing kills the process.
- `_mark_run_complete()` is skipped, so `_should_run_today()` stays True → re-fire every 900 s, but only while
  `now.hour >= 8` UTC (the loop is idle 00:00–07:59). So the worst case is ~64 Grants.gov+LLM rounds/day, not unbounded.
- Every re-fire re-queries the LLM (`_select_opportunities` :322), so a well-formed answer on a later attempt ends the
  loop — retry-until-parse, as the first agent said.
- Paths that *would* end the process: `BaseException` subclasses (`KeyboardInterrupt`, `SystemExit`,
  `asyncio.CancelledError` propagating out of `asyncio.run`) — none is produced by the JSON defect; and compose `restart:
  unless-stopped` (`docker-compose.prod.yml:115`) restarts the container anyway.
- The only code path where `main()` "dies" is the typer one-shot `main` (`:724-745`, no try) — not the prod command.
Verdict: the **issue text is wrong** for the prod configuration; the first agent is right.

**C29c (QUALIFIED).** The first agent's reachability statement ("all pipeline callers are sequential awaits — the only
real concurrency is Phase 4 gather") ignores that the `app` process serves requests concurrently. `src/routers/auth.py:189`
awaits `fetch_orcid_profile` inside the OAuth callback; onboarding/profile routes reach `pubmed._ncbi_get` likewise. N
concurrent users = N concurrent NCBI/ORCID calls through the same `Semaphore(8)`. Verdict unchanged (still present), but
"worst case only under parallel tool calls" understates reachability.

**C27-new NSF (QUALIFIED).** Both regexes are prefix-anchored (`RFA|PAR|PA|NOT|OTA|RFI|DE-FOA` and `PA[RS]?|RFA`), so
*whatever* format Grants.gov returns for NSF, it cannot match unless it starts with one of those. That part is certain.
Whether GrantBot actually posts NSF numbers depends on the LLM selection and the `number` field's shape, which no fixture
in the repo shows — the first agent presented it as fact; it is an inference.

**V10a (QUALIFIED on test coverage).** The fix is real, but no test would fail against the pre-fix `invite.py`:
`grep -rn lookup_user_by_email_async tests/` → `tests/unit/test_slack_web.py` only; `delegate_slack_ids` assertions live in
`tests/integration/test_agent_page.py:835,853` (agent-page link flow). The issue's DoD ("each PR ships a test that fails
against the pre-fix code") is not met for V10 even though the issue marks it done.

**V7c residual.** `scripts/wipe_slack.py:231` `client.conversations_list(types="public_channel", limit=200)["channels"]`
is a single page with no cursor — a destructive script that would silently skip channels past 200. `:168` likewise
single-page history. Out of the issue's scope (agent process), noted for completeness. `scripts/slack_test_teardown.py:48-59`
does paginate.

## 3. Mis-cites by the first agent

None material. Checked: `slack_client.py:324-342, 344-398, 505-550, 662, 1006-1059`; `grantbot.py:341-346, 476-486,
537-538, 615-625, 771-780`; `funding_rules.py:23, 25, 95, 154`; `foa_cache.py:18-21`; `pubmed.py:73, 87-88, 91-96, 434, 439`;
`tools.py:126-148, 205-207`; `invite.py:232-252`; `simulation.py:1457, 1463-1466, 1506, 2777, 3967-3971, 4004-4010`;
`tests/fakes.py:243-254`; `test_slack_client_contract.py:146-169, 172-182, 409, 441-579, 599-659` — all point at the quoted
code. Minor: the report says `_bot_uid_map` at `:4004-4010`; the def is at `:4004` and the "roster first" text is in its
docstring `:4005-4010` — fine. The "84 dead lines" arithmetic (83 code + 1 blank) is correct.

## 4. Counts

**28 rows: Upheld 24 · Overturned 0 · Qualified 3 (C29c reachability, C27-new NSF inference, V10a test coverage) · Unverifiable 1
(V7d API shape — rests on the code being unchanged, not on an observed response).**
