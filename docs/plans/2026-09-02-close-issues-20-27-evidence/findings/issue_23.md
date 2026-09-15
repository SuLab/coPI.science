# Issue #23 verification — External clients: Slack SDK, GrantBot, FOA regexes, HTTP robustness

Verified against `/home/a/scripps/coPI.science` @ `copi-prod` HEAD `18ba52c` (clean tree), 2026-09-02.
Method: symbol-located code, mechanical reproduction with `.venv-test/bin/python` against the real modules,
`git blame`/`git log -S` for provenance, and a run of the DB-free unit tests for the touched modules.

Fix commits cited below are all confirmed ancestors of HEAD (`git merge-base --is-ancestor`): 9dbc9e0, d311170,
02143de, fa143a6, ae123447, a1f9f92f, 18ba52c. The issue's b1d54da is also an ancestor.

## 1. Summary table

| id | claim (one line) | verdict | key evidence | conf |
|---|---|---|---|---|
| V7a | `_call_with_retry` raised `UnboundLocalError` on retry exhaustion — "fixed in stack" | FIXED (9dbc9e0) | `src/agent/slack_client.py:324-342` (`last_exc`); pinned `tests/unit/test_slack_client_contract.py:146-169`; passes | high |
| V7b | `poll_channel_messages` unpaginated — "fixed in stack" | FIXED (d311170) | `slack_client.py:344-398` `_paginate`, `:505-550`, `MAX_PAGES=200` `:133`; incomplete → `[]` `:539-545`; sim cursor per-message `simulation.py:2777` | high |
| V7c | `list_channels` single page — "fixed in stack" | FIXED (d311170) | `slack_client.py:1006-1059`; re-raises `SlackListingIncomplete` `:1043-1053`; tests `:441-579` | high |
| V7d | `resolve_user_name` reads top-level `display_name` (dead branch) | STILL PRESENT | `slack_client.py:662`, unchanged since 1812fa9 (2026-03-20); sibling consumer `agent_page.py:1378` reads `real_name`/`name` only; no unit test; live test passes via fallback | med (shape not network-verified) |
| V7e | `int(Retry-After)` unguarded: HTTP-date → `ValueError` escapes; unclamped | STILL PRESENT — reproduced | `slack_client.py:331,336`; my probe: HTTP-date → `ValueError` escapes `_call_with_retry`; `99999999` → `time.sleep(99999999)` x3; tests feed only `str(int)` (`tests/fakes.py:243-254`) | high |
| C26a | Selection-failure fallback posts unvetted FOAs | STILL PRESENT | `grantbot.py:344-346` returns `list(opportunities.keys())[:max_select]`; downstream caps volume only (`:590-600`) | high |
| C26b | Dict element in LLM JSON → `TypeError: unhashable` at `num in all_opps`, uncaught | STILL PRESENT — reproduced | `grantbot.py:538`; no type-check at `:341-343`; `run_grantbot` `:476-486` has only `finally: dispose` | high |
| C26b' | …so `_mark_run_complete` is skipped and the scheduler re-fires every `check_interval` | STILL PRESENT | `grantbot.py:771-780`: `_mark_run_complete()` only after success; `except Exception` logs → `_should_run_today()` stays True; compose runs `scheduler` (`docker-compose.prod.yml:116`) | high |
| C26b'' | "…and `main()` eventually dies" | NOT REPRODUCIBLE | scheduler's `except Exception` (`:779-780`) swallows and loops; only the one-shot `main` (`:723-745`) has no try, and prod does not run it. Also each re-fire re-queries the LLM, so a later attempt can succeed — it is a retry-until-parse loop, not a hard crash loop | high |
| C26c | GrantBot falls back to SuBot's token → posts authored as SuBot | STILL PRESENT (log level raised in 18ba52c; no code fix) | `grantbot.py:615-625`; engine explicitly refuses to map su's uid to grantbot (`simulation.py:3967-3971`, `_bot_uid_map` `:4004-4010`) | high |
| C26d | `_load_researcher_profiles`/`_extract_list_section`/`_build_search_queries` are 84 dead lines | STILL PRESENT | `grantbot.py:49-131` (83 code lines; 84 with trailing blank); only refs are helper→helper `:68-71`; no callers in src/scripts/tests; `PROFILES_DIR` `:44` is dead with them | high |
| C26e | "Already fixed earlier: two-phase `_claim_foa`/`_release_foa`" | FIXED (confirmed) | `grantbot.py:246-275`; used `:658,:673,:681,:691` | high |
| C27a | 6-row regex divergence table | STILL PRESENT — all 6 rows reproduce exactly | run output below; `foa_cache.py:19-21`, `funding_rules.py:95` | high |
| C27b | Neither regex is `IGNORECASE` | STILL PRESENT | `flags & IGNORECASE == False` for both; all lowercase variants fail both | high |
| C27c | `foa_cache.py:18` docstring falsified on 2 of its 3 examples; `extract_foa_number` → `None` for PA/PAR/PAS | STILL PRESENT | `extract_foa_number("...PAR-24-293...")` → `None`; `DE-FOA-0003456` → `None`; `RFA-AI-27-019` → ok. Callers `simulation.py:1119,1217,1258` | high |
| C28a | Apostrophe classes ASCII-only; U+2019 form passes the announcement filter | STILL PRESENT — reproduced | `funding_rules.py:23` class is `['']` = U+0027 twice (codepoints verified); `:25` `'?`; `"I'll spin up…"`→True, `"I’ll spin up…"`→False | high |
| C28b | Ack-only detector false-rejects substantive short replies | STILL PRESENT — reproduced | `is_acknowledgment_only_funding_reply("Agreed, we can send the plasmids and the mice next week.")` → `True` | high |
| C28b' | `funding_reject_count` resets only on successful post; one-strike mode after 2 rejections | STILL PRESENT | reset only at `simulation.py:1506`; increment `:1457`; back-off `:1463-1466`. Issue's `:1465` drifted → `:1506`; "three sites" re-arm count is stale (≥10 sites) | high |
| C28c | `_TAG_RE` lacks `IGNORECASE` (`@grantbot` ok, `@GRANTBOT`/`@SuBOT` miss) | STILL PRESENT — reproduced | `funding_rules.py:154`; results below | high |
| C28d | Cross-issue: mirrors issue #20 E5's `_extract_tagged_agent` fix | N/A (cross-ref) — but note: `message_log.py:407` is ALSO still case-sensitive; the two are "in sync" only in that neither is fixed. Same literal also at `simulation.py:2523,2558` | high |
| C29a | No retry/backoff in `orcid.py`/`pubmed.py`/`grants.py` | STILL PRESENT | `grep retry\|backoff\|tenacity` → nothing; pyproject has no tenacity; every call is `httpx` + `raise_for_status()` | high |
| C29b | `raise_for_status()` before the pacing sleep | STILL PRESENT | `pubmed.py:93-95`; `tests/contract/test_pubmed_contract.py:5` documents "sleeps per *successful* call" | high |
| C29c | `Semaphore(8)` never sized to key/no-key; `api_key` check does not feed back | STILL PRESENT (impact qualified) | `pubmed.py:73`, `:87-88`; all pipeline callers are sequential `await`s — the only real concurrency is Phase 4 `asyncio.gather` (`simulation.py:1328-1332`); semaphore is process-local across app/worker/agent | high |
| C29d | PR #32 edited `_ncbi_get` (tool/email) without touching rate math | confirmed (context) | `pubmed.py:76-90` | high |
| C30 | Budget debited before the awaited fetch; blanket `except` → no refund | STILL PRESENT | `tools.py:126-129`, `:135-138`, `:146-148`; no `-=` anywhere in src/. Sharper: a *non-raising* miss (`_execute_retrieve_abstract` `:205-207` returns `result["error"]`) also consumes budget | high |
| V10a | Invite Slack sync imports fixed (`token_for_agent_row` + `lookup_user_by_email_async`), logs instead of `pass` | FIXED (02143de; async wrapper via fa143a6) | `src/routers/invite.py:232-252`; `slack_tokens.py:46` | high |
| V10b | Residual nit: a `None` token skips the block with no log line | STILL PRESENT (nit) | `invite.py:237-238` `if bot_token:` with no else | high |

Counts: **18 still present, 5 fixed, 0 partial, 0 changed, 1 not reproducible, 2 N/A/context.**

Tests run (host, no DB): `tests/unit/test_slack_client_contract.py tests/unit/test_funding_rules.py
tests/unit/test_grantbot_lead_time.py tests/unit/test_slack_tokens.py tests/unit/test_service_bot_attribution.py
tests/unit/test_slack_web.py` → **170 passed in 8.16s**. None of these asserts the fixed behaviour for any
STILL PRESENT item above (details per item).

---

## 2. Per-item detail

### V7 — Slack client (`src/agent/slack_client.py`)

**V7a UnboundLocalError — FIXED.** Current code:

```
324	        last_exc: SlackApiError | None = None
325	        for attempt in range(MAX_RETRIES):
326	            try:
327	                return method(**kwargs)
328	            except SlackApiError as exc:
329	                if exc.response.get("error") == "ratelimited":
330	                    last_exc = exc
331	                    retry_after = int(exc.response.headers.get("Retry-After", 5))
...
336	                    time.sleep(retry_after)
337	                else:
338	                    raise
339	        raise SlackApiError(
340	            "Rate limit retries exhausted",
341	            response=last_exc.response if last_exc else None,
342	        )
```
`git log -S"last_exc"` → 9dbc9e0 ("Slack T2: client wire contract…"). Regression test
`test_retries_are_bounded_and_raise_a_SlackApiError` at `tests/unit/test_slack_client_contract.py:146-169` — the
issue's line range is exact. Passes.

**V7b / V7c pagination — FIXED.** `_paginate` (`:344-398`) walks `response_metadata.next_cursor`, bounded by
`MAX_PAGES = 200` (`:133`), detects repeated cursors, raises `SlackListingIncomplete` with the partial. `git log -S"def _paginate"`
→ d311170. `poll_channel_messages` (`:505-550`) returns `[]` on `SlackListingIncomplete` (`:539-545`); the sim advances
`_poll_cursors[ch_id] = ts` per processed message (`simulation.py:2776-2777`), so the issue's "closed at both ends" holds.
`list_channels` (`:1006-1059`) caches the partial then re-raises (`:1043-1053`). Tests: `:409` (every cursor read goes
through `_paginate`), `:441-579` (list_channels), `:599-659` (poll). All pass. Issue line refs `:344-398`, `:505-545`,
`:1006-1059` are still exact — this file region has not drifted since b1d54da.

**V7d `resolve_user_name` — STILL PRESENT.**
```
655	    def resolve_user_name(self, user_id: str) -> str:
...
660	            info = self._api("users_info", user=user_id)
661	            user = info.get("user", {})
662	            return user.get("display_name") or user.get("real_name") or user_id
```
`git blame` → 1812fa9 (2026-03-20), never touched. Slack's `users.info` puts `display_name` under `user.profile`
(top-level has `name`, `real_name`); I could not call Slack to re-confirm (constraint), but the codebase's other
consumer of the same object — `src/routers/agent_page.py:1378` `info.get("real_name") or info.get("name") or sid` —
reads exactly the top-level keys that exist and does not attempt `display_name`. Callers are live paths
(`simulation.py:2781` channel poll of human posts, `:3222` thread PI replies). No unit test covers
`resolve_user_name`; the live test `tests/integration/test_slack_client_live.py:47-51` only asserts the result is not the raw
id, which the `real_name` fallback satisfies — it would not catch the dead branch.

**V7e Retry-After — STILL PRESENT; reproduced.** Probe (`AgentSlackClient.__new__`, `time.sleep` mocked, fake
`SlackResponse` with `error=ratelimited`):
```
HTTP-date -> ESCAPES as ValueError : invalid literal for int() with base 10: 'Wed, 21 Oct 2015 07:28:00 GMT'
huge int  -> sleep calls: [99999999, 99999999, 99999999]
negative  -> sleep calls: [-5, -5, -5]      (real time.sleep(-5) raises ValueError too)
```
So a non-integer header raises `ValueError` out of the `except SlackApiError` block; `post_message`'s
`except SlackApiError` never sees it — the same type-substitution class V7a fixed. No clamp. Tests only ever build
`{"Retry-After": str(retry_after)}` with `retry_after: int | None` (`tests/fakes.py:243-256`);
`test_retry_after_header_is_honoured` (`:172-182`) feeds `17`. Mitigation that exists *elsewhere*: the sibling
`src/services/slack_web.py:_call` (`:104-120`) already does `float()` in `try/except (TypeError, ValueError)` and caps at
`_MAX_RETRY_AFTER = 30.0` (`:49`), with tests `tests/unit/test_slack_web.py:191-218`. That is the pattern to port; the
`slack_client.py` path (the agent engine) does not have it.

### V8 — GrantBot (`src/agent/grantbot.py`), FOA regexes, funding detection

**COR-26a fallback — STILL PRESENT.**
```
341	        selected = json.loads(cleaned)
342	        logger.info("Selected %d of %d opportunities", len(selected), len(opportunities))
343	        return selected[:max_select]
344	    except Exception as exc:
345	        logger.warning("Selection failed: %s — falling back to all", exc)
346	        return list(opportunities.keys())[:max_select]
```
Blame a1f9f92f (2026-03-28). Downstream `:590-600` caps by `max_per_channel` (prod: 1) and `max_posts` (10) — a
volume cap, not a relevance filter, so up to 10 arbitrary FOAs (first 30 keys in Grants.gov order, one per LLM-chosen
channel) get drafted and posted. Extra observation: a non-list JSON (`{}`, `123`, `null`) fails at `selected[:max_select]`
*inside* the try and lands in the same fallback; a bare JSON string (`"PAR-24-293"`) passes the slice and is returned
as a `str`, which `:538` then iterates character by character → 0 selected, silently.

**COR-26b TypeError — STILL PRESENT; reproduced.**
```
537	    selected_nums = await _select_opportunities(all_opps)
538	    selected_opps = {num: all_opps[num] for num in selected_nums if num in all_opps}
```
```
selected=['PAR-24-293', {'number': 'RFA-AI-27-019'}] -> TypeError: unhashable type: 'dict'
selected=['PAR-24-293', ['RFA-AI-27-019']]           -> TypeError: unhashable type: 'list'
selected=['PAR-24-293', 42, None]                     -> OK ['PAR-24-293']
```
No `isinstance` check on the parsed list anywhere in `_select_opportunities` (`:341-343`); no try around `:537-538` in
`_run_grantbot_with_session`; `run_grantbot` (`:476-486`) is `try/finally: engine.dispose()` only.

**COR-26b' re-fire — STILL PRESENT; COR-26b'' "main() dies" — NOT REPRODUCIBLE.**
```
767	    while True:
769	        if _should_run_today() and now.hour >= run_hour:
771	            try:
772	                results = asyncio.run(run_grantbot(...))
777	                _mark_run_complete()
779	            except Exception as exc:
780	                logger.error("Daily run failed: %s", exc, exc_info=True)
786	        time.sleep(check_interval)
```
`_mark_run_complete()` runs only on success, so a failed run leaves `_should_run_today()` True and the scheduler
retries every `check_interval` (default 900 s; `docker-compose.prod.yml:116` passes no override). But the scheduler
does **not** die — `except Exception` swallows it. The one-shot `main()` (`:723-745`) has no try and would exit non-zero,
but prod runs `scheduler`. Also: each re-fire re-queries Grants.gov and the LLM, so a later attempt can parse cleanly;
the failure mode is an unbounded retry-until-parse loop (cost + log noise), not a permanent crash loop.

**COR-26c SuBot fallback — STILL PRESENT (log raised to WARNING in 18ba52c).**
```
615	            candidate = getattr(settings, "slack_bot_token_grantbot", "")
616	            if not candidate or candidate.startswith("xoxb-placeholder"):
617	                candidate = settings.slack_bot_token_su
622	                logger.warning(
623	                    "No grantbot Slack token — using SuBot's token as fallback; "
624	                    "these posts will be attributed to su, not grantbot",
```
Blame ae123447 (2026-08-04) for the lines, 18ba52c for the WARNING. The engine side now deliberately does *not* map
su's uid to grantbot (`simulation.py:3967-3971`; `_bot_uid_map` `:4004-4010` "roster clients first"), so fallback posts
attribute to `su`, as the issue says. `tests/integration/test_grantbot_live.py:270-283` (`_SettingsWithFakeToken`)
returns the same fake for both token names — no test distinguishes the fallback.

**COR-26d dead helpers — STILL PRESENT.** `_load_researcher_profiles` `:49-76`, `_extract_list_section` `:79-96`,
`_build_search_queries` `:99-131`. `grep -rn` over src/ scripts/ tests/ docs/ specs/: the only references are the
in-file helper→helper calls at `:68-71` and a docs inventory line (`docs/superpowers/plans/2026-08-12-branch2-inventories/branch2-inventory-funding.md:23`,
"no external importers"). 83 code lines (`:49-131`); the issue's `:49-132` = 84 counts one blank. `PROFILES_DIR` (`:44`)
has no other user either.

**COR-26e** two-phase claim/release — present at `:246-275`, used at `:658/:673/:681/:691`. Matches the issue's
"already fixed".

**COR-27 regex table — STILL PRESENT; all 6 rows reproduce.** Run against the real modules:
```
FOA_PATTERN    = \b((?:RFA|PAR|PA|NOT|OTA|RFI|DE-FOA)-[A-Z]{2,4}-\d{2,4}-\d{2,5})\b | IGNORECASE: False
_FOA_NUMBER_RE = \b(PA[RS]?-\d{2}-\d{3,4}|RFA-[A-Z]{2,3}-\d{2}-\d{3,4})\b          | IGNORECASE: False

input             foa_cache.FOA_PATTERN   funding_rules._FOA_NUMBER_RE  extract_foa_number(sentence)
PAR-24-293        False                   True                          None
par-24-293        False                   False                         None
PA-24-293         False                   True                          None
pa-24-293         False                   False                         None
PAS-24-293        False                   True                          None
pas-24-293        False                   False                         None
NOT-OD-24-001     True                    False                         'NOT-OD-24-001'
not-od-24-001     False                   False                         None
DE-FOA-0003456    False                   False                         None
de-foa-0003456    False                   False                         None
RFA-AI-27-019     True                    True                          'RFA-AI-27-019'
rfa-ai-27-019     False                   False                         None
```
Blame: `foa_cache.py:20` → 12dbefe (2026-04-03); `funding_rules.py:95` → 03af17e (2026-04-13); neither touched since.
Docstring `foa_cache.py:18` lists `RFA-AI-27-019, PAR-24-293, DE-FOA-0003456`; 2 of 3 fail its own regex.
Consequence path: `simulation.py:1119,1217,1258` call `extract_foa_number` on GrantBot roots; for every PA/PAR/PAS
post `thread.foa_number` stays `None` and `format_foa_for_prompt` never resolves. Tests: no unit test imports
`FOA_PATTERN`/`extract_foa_number`; `tests/unit/test_funding_rules.py` only ever uses `PAR-25-297`, which
`_FOA_NUMBER_RE` matches — so the suite is green on the divergent pair. Additional gap the issue does not mention:
GrantBot searches `BIOMEDICAL_AGENCIES = ["HHS-NIH11", "NSF"]` (`grants.py:14`); NSF numbers (e.g. `25-543`) match
*neither* regex. (`data/foa_cache/` does not exist in this checkout, so I could not measure the real posted distribution.)

**COR-28a apostrophes — STILL PRESENT; reproduced.** `funding_rules.py:23` raw pattern
`"\\bi['']?ll (start|…)\\b"`; the class contains codepoints `['0x27', '0x27']` — ASCII twice, no U+2019. `:25` is `i'?m`.
```
"I'll spin up a dedicated thread."   announcement_only=True
'I’ll spin up a dedicated thread.'   announcement_only=False
"I'm going to start a new thread."   announcement_only=True
'I’m going to start a new thread.'   announcement_only=False
```
`TestAnnouncementOnly` (`tests/unit/test_funding_rules.py:34-70`) uses ASCII apostrophes only.

**COR-28b ack detector — STILL PRESENT; reproduced.**
```
'Agreed, we can send the plasmids and the mice next week.'   ack_only=True   (REJECTED)
'Agreed, … next week — aim 2 fits.'                          ack_only=False
```
`_SUBSTANTIVE_MARKERS_RE` (`:43-49`) has `mouse model` but not `mice`/`plasmid`/`send`. Reset semantics:
`funding_reject_count` is incremented at `simulation.py:1457`, triggers `has_pending_reply = False` at `:1463-1466`
when `>= 2`, and is reset **only** at `:1506` inside the successful-post branch (the issue's `:1465` has drifted to
`:1506`). `has_pending_reply` is re-armed at ≥10 sites (`:1223, :1264, :1315, :2827, :2932, :2987, :2999, :4242,
:5129, :5141`), not "three" — the count in the issue is stale but the mechanism (one-strike mode after two rejections
until a post succeeds) is exactly as described.

**COR-28c `_TAG_RE` — STILL PRESENT; reproduced.** `funding_rules.py:154` `@(\w+[Bb]ot)\b`, no IGNORECASE:
`@grantbot`→`grantbot`, `@GRANTBOT`→None, `@SuBOT`→None, `@SuBot`→ok, `@Subot`→ok.
Cross-issue note: `src/agent/message_log.py:407` `_extract_tagged_agent` uses the identical case-sensitive literal, as do
`simulation.py:2523` and `:2558` and the live test `test_grantbot_live.py:579`. Whatever issue #20 E5 proposed has not
landed at HEAD; there are four production copies of this regex, none IGNORECASE.

### V9 — HTTP robustness (`src/services/orcid.py`, `pubmed.py`, `grants.py`) + tool charging (`src/agent/tools.py`)

**COR-29a — STILL PRESENT.** `grep -rn "retry\|backoff\|tenacity"` across the three files → no matches (exit 1);
`pyproject.toml` has `httpx>=0.27.0` and no tenacity. Every request is `client.get/post(...)` + `resp.raise_for_status()`:
`orcid.py:18-19, :82-83, :104-105`; `grants.py:40-41, :94-95, :130-131`; `pubmed.py:93-94`. Callers only ever
`except Exception: log; continue` (`pubmed.py:114, :140, :290, :309, :337, :358`; `orcid.py:85, :107`; `grants.py:212`).

**COR-29b — STILL PRESENT.**
```
91	    async with _request_semaphore:
92	        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
93	            resp = await client.get(url, params=params)
94	            resp.raise_for_status()
95	            await asyncio.sleep(0.12)  # ~8 req/s
96	            return resp
```
Blame 5972103 (2026-03-20). On a 429/5xx the sleep is skipped and the caller's `except` moves straight to the next
request. `tests/contract/test_pubmed_contract.py:5` says "_ncbi_get sleeps ~0.12s per *successful* call" — the test
suite documents the defect rather than asserting against it.

**COR-29c Semaphore sizing — STILL PRESENT, impact qualified.** `pubmed.py:73` `asyncio.Semaphore(8)`; `:87-88`
`if settings.ncbi_api_key: params["api_key"] = …` — nothing sizes the semaphore. In practice: `profile_pipeline.py`
(`:81-268`), `cli.py:48`, `auth.py:189`, `scripts/generate_sparsedata_user.py` all `await` sequentially; the only
concurrency that can reach `_ncbi_get` is Phase 4 `asyncio.gather` over an agent's threads (`simulation.py:1328-1332`)
→ `execute_tool` → `fetch_abstract`. So "tens of req/s" is the worst case under parallel tool calls, not the steady
state. Not mentioned by the issue: the semaphore is process-local — app, worker and agent containers each get their
own 8, and NCBI's limit is per key/IP. PR #32's `tool`/`email` edit (`:76-90`) confirmed; rate math untouched.

**COR-30 — STILL PRESENT.**
```
126	                if thread_state.abstracts_other >= settings.max_abstracts_other_per_thread:
127	                    return "Rate limit: …"
128	                thread_state.abstracts_other += 1
129	            return await _execute_retrieve_abstract(tool_input["pmid_or_doi"])
...
137	                thread_state.full_text += 1
138	            return await _execute_retrieve_full_text(tool_input["pmid_or_doi"])
...
146	    except Exception as exc:
147	        logger.error("Tool execution failed: %s(%s) — %s", tool_name, tool_input, exc)
148	        return f"Error executing {tool_name}: {exc}"
```
`grep -rn "abstracts_other -=\|full_text -="` → nothing. Sharper than filed: `fetch_abstract` returns
`{"error": …}` without raising for "could not resolve DOI"/"no record" (`pubmed.py:434, :439`) and
`_execute_retrieve_abstract` (`tools.py:205-207`) returns that string — budget is consumed on those non-exception
misses too. Also the comment at `:122-123` ("We don't enforce limits on own-lab lookups") does not match the code,
which increments for every `retrieve_abstract` regardless of whose paper it is. No test in `tests/unit/` touches
`abstracts_other`.

### V10 — invite import (`src/routers/invite.py`)

**FIXED.** `git log -S"token_for_agent_row" -- src/routers/invite.py` → 02143de replaced
`from src.routers.agent_page import _get_bot_token` + raw `WebClient` + bare `except: pass` with
`token_for_agent_row(agent)` + `lookup_user_by_email` + `logger.warning`; fa143a6 switched to
`lookup_user_by_email_async`. Current `:232-252` matches the issue's description and line range exactly.
`token_for_agent_row` is at `src/services/slack_tokens.py:46`. **Residual nit confirmed:** `:237-238`
`bot_token = token_for_agent_row(agent); if bot_token:` — a `None` token (no DB token, no `.env` token) skips the
whole lookup silently; the only log is on exception.

---

## 3. Counts

**18 still present, 5 fixed, 0 partially fixed, 0 changed, 1 not reproducible** (+2 cross-reference/context rows).

Where the issue text is stale or wrong (separate from the defects):
- "…and `main()` eventually dies" (COR-26b): the prod `scheduler` catches `Exception` and keeps looping; nothing dies.
- `simulation.py:1465` → now `:1506`; "`has_pending_reply` re-arms at three sites" → ≥10 sites.
- `_load_researcher_profiles…:49-132` "84 dead lines" → 83 code lines `:49-131` (84 with the blank).
- All other line references (`slack_client.py`, `grantbot.py`, `funding_rules.py`, `pubmed.py`, `tools.py`, `invite.py`)
  are still exact at HEAD — none of these regions moved after b1d54da.
- The "cross-issue #20 E5" mirror: nothing case-insensitive has landed in `_extract_tagged_agent` either.

## 4. What I could not verify and why

- **Slack `users.info` response shape** (V7d): constraint forbids network calls. Verdict rests on the code being
  unchanged since 2026-03-20, on the sibling consumer `agent_page.py:1378` reading only `real_name`/`name`, and on
  the documented API shape (`user.profile.display_name`). Confidence medium.
- **Real posted-FOA number distribution** (COR-27 impact): `data/foa_cache/` and `data/grantbot_last_run.txt` are not
  present in this checkout (prod-only volume), so I could not count how many real posts hit the PA/PAR gap.
- **Live NCBI rate behaviour** (COR-29): not exercised; the concurrency analysis is static.
- **Contract/integration tests** (`tests/contract/test_pubmed_contract.py`, `tests/integration/test_grantbot_live.py`)
  were read, not run, per the preamble.

Scratch scripts used: `/tmp/claude-1000/-home-a-scripps-coPI-science/c8a1ec5c-25b0-4282-b6cd-c2567cafee26/scratchpad/23/{cor27,cor28,cor26,v7_retry,realfoas}.py`.
