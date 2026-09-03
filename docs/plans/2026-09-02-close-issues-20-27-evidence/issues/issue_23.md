# #23 External clients: Slack SDK, GrantBot, FOA regexes, HTTP robustness (3 PRs)
state=OPEN created=2026-07-30T15:53:06Z updated=2026-08-11T23:54:42Z labels=['area:integrations', 'verified-2026-07-30']

## BODY

Verified defects in the outbound integrations: the Slack client wrapper, GrantBot + the funding regexes, and the ORCID/PubMed/Grants HTTP layer.

Originally verified at `origin/main` @ `b7edcbc` (2026-07-30). **Re-verified 2026-08-11 against the open PR-stack tip (`issue-29-authorship-grounding` @ `b1d54da` = main + #30/#31/#32). The stack fixed the Slack retry/pagination cluster and the invite import (V10) — marked below; `grantbot.py`, `foa_cache.py` and `funding_rules.py` are untouched by it.**

## Priority (triage 2026-08-11)

- **V9 rises to Tier 1-adjacent:** the ORCID/PubMed transport is exactly the path the issue-#29 publications backfill (unmuting the 11 zero-publication labs) will hammer, and PR #32 already edited `_ncbi_get` (added `tool`/`email`) without touching the rate math. Land with issue #22's V1 (V1 = parsing, V9 = transport; V1 first keeps the diffs disjoint).
- **V7 remainder** is two trivial items. **V8** is Tier 3 funding-feature correctness — its sharpest edge is COR-26b, a crash *loop* (the failed run never marks complete, so the scheduler re-fires it every interval).

**Suggested order: V9 → V7 remainder → V8.** V10 is done.

### PR V7 — Slack client correctness cluster *(small; mostly fixed in stack)*
All in `src/agent/slack_client.py`:
- ~~`_call_with_retry` raises `UnboundLocalError` on rate-limit exhaustion~~ — **fixed in stack** (`last_exc` alias, `:324-342`; regression-pinned at `tests/unit/test_slack_client_contract.py:146-169`).
- ~~`poll_channel_messages` has no pagination~~ — **fixed in stack**: cursor-paginated via `_paginate` (`:344-398`, `:505-545`) with `MAX_PAGES=200`; on an incomplete listing it returns `[]` and the sim's cursor only advances per processed message, so the >100-message-gap skip is closed at both ends.
- ~~`list_channels` is a single page~~ — **fixed in stack** (`:1006-1059`; re-raises on incomplete rather than returning a subset that looks complete).
- `resolve_user_name` reads top-level `display_name` (`:662`) — **still present**; Slack returns it at `profile.display_name`, so the first branch is dead and every call degrades to `real_name`/`user_id`.
- `int(Retry-After)` (`:331`) — **still present**, and sharper than filed: a non-integer header (RFC 7231 permits an HTTP-date; proxies inject them) raises `ValueError` *from inside the `except SlackApiError` block*, so callers' `except SlackApiError` never sees it — the same type-substitution class the `UnboundLocalError` fix just closed. Also unclamped: an arbitrarily large value blocks in `time.sleep`. Tests only ever feed `str(int)`.

*Fix (remainder):* read `profile.display_name`; guard + clamp `Retry-After`.

### PR V8 — GrantBot + FOA regex + funding detection *(small-medium; untouched by the stack)*
- **COR-26 (remainder)** — all four still present: the selection-failure fallback posts **unvetted** FOAs (`grantbot.py:344-346` returns up to `max_select` arbitrary keys on any exception; downstream caps volume, not relevance); a dict element in the LLM's JSON hits `num in all_opps` → `TypeError: unhashable type` (`:538`), uncaught in `run_grantbot` — and because `_mark_run_complete()` is then skipped, **the scheduler re-fires the crash every `check_interval`** and `main()` eventually dies; GrantBot falls back to SuBot's token (`:615-620`), so funding posts are authored under SuBot's identity; `_load_researcher_profiles`/`_extract_list_section`/`_build_search_queries` (`:49-132`) are 84 dead lines with no callers. *(Already fixed earlier: two-phase `_claim_foa`/`_release_foa`.)* *Fix:* hard-fail selection; type-check the parsed list; drop the dead helpers.
- **COR-27** — still present; both patterns unchanged and divergent **in both directions**:

  | input | `foa_cache.FOA_PATTERN` | `funding_rules._FOA_NUMBER_RE` |
  |---|---|---|
  | `PAR-24-293` | ✗ | ✓ |
  | `PA-24-293` | ✗ | ✓ |
  | `PAS-24-293` | ✗ | ✓ |
  | `NOT-OD-24-001` | ✓ | ✗ |
  | `DE-FOA-0003456` | ✗ | ✗ |
  | `RFA-AI-27-019` | ✓ | ✓ |

  Neither is `IGNORECASE`. `foa_cache.py:19-21` forces a `-[A-Z]{2,4}-` institute segment, rejecting the entire PA/PAR/PAS parent-announcement family — the docstring at `:18` is falsified by its own regex on 2 of its 3 examples, and `extract_foa_number` returns `None` for those posts so cache/prompt keys never resolve. *Fix:* one correct shared pattern accepting PA/PAR/PAS/RFA/NOT/DE-FOA with 2- and 4-digit years, used by both call sites.
- **COR-28** — still present:
  - The apostrophe classes in `_ANNOUNCEMENT_PHRASES` are ASCII-only in both phrases (`funding_rules.py:23` — the class is the ASCII apostrophe listed *twice* — and `:25`). Verified live: `"I'll spin up a dedicated thread."` → filtered; the U+2019 curly form → **passes**. LLM output and Slack smart-quotes routinely produce U+2019.
  - `is_acknowledgment_only_funding_reply` (`:108-134`) false-rejects substantive short replies whose vocabulary is outside the fixed 28-term marker list (verified: `"Agreed, we can send the plasmids and the mice next week."` → rejected). The soft-livelock is sharper than filed: `funding_reject_count` resets **only on a successful post** (`simulation.py:1465`) while `has_pending_reply` re-arms at three sites — after two rejections a thread is permanently in one-strike mode.
  - `_TAG_RE` (`:154`) lacks `IGNORECASE` — partially mitigated by `[Bb]` (`@grantbot` matches; `@GRANTBOT`/`@SuBOT` don't).
  *Fix:* fix both apostrophe classes; broaden the ack detector (or reset the counter on any turn); add `IGNORECASE` to `_TAG_RE`.
  - **Cross-issue:** mirrors PR E5's `_extract_tagged_agent` fix in issue #20. Land one shared helper or keep the two in sync.

### PR V9 — External-HTTP robustness + tool counter charging *(small-medium)*
- **COR-29 (robustness slice)** — still present, all three: none of `orcid.py`/`pubmed.py`/`grants.py` contains any retry/backoff (every call is a one-shot `httpx` + `raise_for_status()`); `raise_for_status()` runs before the pacing sleep (`pubmed.py:93-95`), so pacing is skipped exactly when NCBI is 429-ing; `Semaphore(8)` (`pubmed.py:73`) with the 0.12 s sleep *inside* the semaphore yields tens of req/s against the keyless 3/s limit — the `api_key` presence check exists (`:87-88`) but never feeds back into concurrency. PR #32 touched `_ncbi_get` (per-policy `tool`/`email` params, `:76-90`) without addressing any of this. *Fix:* retry/backoff; sleep before `raise_for_status`; size the semaphore to the key/no-key limit. *(Shared `AsyncClient` + `gather` parallelization remain deferred to the refactor pass.)*
  - **Cross-issue:** same two files as issue #22's V1 — V1 fixes parsing, V9 fixes transport; land V1 first. Both are prerequisites for the issue-#29 publications backfill.
- **COR-30** — still present, unchanged by #32 (its diff touches only the docstring and the authors/DOI formatting): `tools.py:126-129/:135-138` debit the abstract/full-text budget **before** the awaited fetch, and the blanket `except` at `:146` converts a failure into an error string with no refund — a failed retrieval still consumes budget. *Fix:* increment only on success.

### ~~PR V10 — Fix the broken invite import~~ *(fixed in stack)*
`invite.py:232-252` now imports `token_for_agent_row` (`src/services/slack_tokens.py:46`) + `lookup_user_by_email_async`, and the handler logs instead of `pass`. (Resolved per-agent rather than via `get_any_bot_token` — better: the lookup uses the inviting agent's own bot.) Residual nit for whoever touches the file next: a `None` token skips the block with no log line.

**Definition of done:** each PR ships a test that fails against the pre-fix code. The regex fixes should be table-driven over the cases above.


