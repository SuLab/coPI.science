# CLAUDE.md claims falsified by branch f3171bb..3f9b6a5

From the final whole-branch review. Each row: the claim, why the branch falsified it, the new truth.

| Claim (CLAUDE.md line) | Why the branch falsified it | New truth |
|---|---|---|
| L125 `# Fresh run (wipes agent_messages/channels, keeps proposals):` | `src/agent/main.py:99` `_open_fresh_run` — "DELETES NOTHING"; the three unfiltered deletes are gone (commit `4c49e62`) | `--fresh` mints a new `simulation_run_id` and deletes nothing; isolation is the run id, and pre-run Slack history is skipped rather than re-imported. Rows now accumulate across fresh runs. |
| L132-137 `--budget` "cumulative cap ... rebuilt from `llm_call_logs` on restart"; the rate-limiter paragraph | `simulation.py:141,6791,6866` rebuild per call; `:6914` books tool rounds live | Both `--budget` and the sliding-window allowance are denominated in **real API calls** now. Any previously-tuned number is ~2-3x tighter; `llm_calls_per_load_per_window=8` was NOT re-tuned. |
| L152-153 "a single `thread_reply` turn runs up to 134s (up to `max_tool_rounds` real API calls...)" | `src/services/llm.py:83` corrected 1..7 -> **1..8**; the loop is `range(max_tool_rounds + 1)`, and the forced-final warning now prints `+ 1`. Concurrency also changed (`_API_MAX_CONCURRENCY=8`, `_API_EXECUTOR_MAX_WORKERS=12`) | A turn is up to `max_tool_rounds + 1` tool-capable calls plus a forced final plus one retry — 8 at the default. B1.5 mandated this CLAUDE.md edit specifically; it was not made. |
| L174 "Exit 137 means SIGKILL and **a lost flush**" | `simulation.py:1053` `_drain_and_flush` in the loop's `finally`; `stop()` gathers `_flush_tasks` then flushes with `final=True`; plus the never-shut-down API executor | Exit 137 no longer implies a lost flush. The reliable check is `"Simulation stopping..."`, logged after the final flushes; a 137 AFTER that line is an orphaned API thread (up to `CLIENT_READ_TIMEOUT_SECONDS`=300s), not data loss. |
| L183-189 restart recipe: `up -d --build` **then** `alembic upgrade head` | 0036 maps `panel_owed`, `thread_id`, `truncated`, and two `llm_call_logs` columns; new code against 0035 raises `UndefinedColumn` (`0036_*.py:92-97`) | For this deploy: build -> `$DC run --rm blackbird-app alembic upgrade head` -> verify `current == heads == 0036` -> `up -d blackbird-app worker` -> `--profile agent build agent`. CLAUDE.md has boxes for 0028/0030/0031 and **none for 0036**. |
| L304 "deferred to a separate later migration (`0031`+ — `0030` is now taken...)" | head is 0036 | 0031-0036 are all taken; the `users.is_admin` drop is still unwritten. |
| L348-350 "The PI-write POSTs (`/onboarding/save-profile`, `/onboarding/retry`, `/profile/refresh`, `/agent/request`) are gated on `get_pi_user`" | `src/routers/profile.py:113` moved `POST /profile/save` to `get_pi_user` (E1.3, commit `5ca802c`) | FIVE PI-write POSTs; `POST /profile/save` is the fifth, and it was the one a manager could use to rewrite `users.email`. |
| L524-528 `search_prior_art` bullet | `src/services/patents.py` `_to_ascii`/`_prepare`, `_QUERY_OPERATORS`, `PriorArtResult.dropped_or_rewritten`; `tools.py:_rewrite_note` | Still title-only and still "an empty title search is never FTO", but queries are now transliterated (Greek spelled out, Unicode dashes folded), `AND/OR/NOT` are dropped as syntax, and every change is disclosed to the model. The tool description the model sees also changed. |
| L513-518 `weighted_score` bullet | `src/models/opportunity.py:147-164`; `simulation.py:3631` | The verdict row now carries a fourth write-time fact, `panel_owed`, and the read path REPLAYS it instead of recomputing (`assessment_detail.panel_state`, five states). |
| L71 "469 of them, measured 2026-08-04" | +14 test files, many DB-backed | Stale count. |
| L382 "`prompts/` is bind-mounted into exactly the two services that read it, `blackbird-app` and `agent`" | True only in the **uncommitted** working copy of `docker-compose.prod.yml` (`app` -> `blackbird-app` rename, `copi-edge` alias, awslogs -> json-file) | Every `blackbird-app` command in CLAUDE.md depends on an uncommitted local edit. Not this branch's doing, but a live deploy dependency. |

## Not this branch's doing, but false at HEAD and load-bearing — verify before trusting

- **L429/L435** — `premature_sidecar` is HISTORICAL ONLY as of `f94b363`; `_sidecar_refusal` now returns only `duplicate_thread_verdict` and stores non-terminal sidecars as provisional. This branch EXTENDS that across restarts via `thread_id` + `_rehydrate_assessed_threads`.
- **L495** — "every HELD verdict ... does additionally trigger" the `#assessments-summary` post. Only a **terminal, not-yet-announced** verdict does (`simulation.py:3235-3262`).
- **L50-60** — the SDK's non-streaming `max_tokens` guard no longer fires, because `_client_for_key` passes an explicit timeout (`src/services/llm.py:64-76`). `_acreate`'s own check, now raising `NonStreamingMaxTokensError`, is the only enforcement.

## Not in CLAUDE.md at all, and an operator must know

- The Origin guard 403s every non-GET without a matching `Origin` / `Sec-Fetch-Site: same-origin`. `curl -X POST` against the app now needs `-H "Origin: $BASE_URL"`, and **a wrong or missing `BASE_URL` fails closed site-wide**.
- `/docs`, `/redoc` and `/openapi.json` now 404.
- Revoking access ends the session immediately. Admins can still impersonate a denied account — deliberate, a support path, commented at the check in `src/dependencies.py`.
- The startup banner has a third line: the API-call units note.
- `AssessmentDrop.reason` gained `unwritable_row`.
- The assessments banner will JUMP to include all 64 historical rows on first load (`panel_owed IS NULL`, deliberately never backfilled). Correct, not a regression — but staff read that page daily.
- 0036 also swaps `private_channel_members_user_id_fkey` to CASCADE (brief ACCESS EXCLUSIVE lock) and fixes `POST /profile/delete-account` 500ing for any private-channel member.
