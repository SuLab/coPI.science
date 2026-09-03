# Issue #24 verification — "Web request-path robustness: concurrent-insert 500s and a 5-minute event-loop freeze"

Verified against `/home/a/scripps/coPI.science` @ `copi-prod` HEAD `18ba52c` (clean tree), 2026-09-02.
Method: symbol lookup + quoted code, AST inspection of the handlers, git blame/ancestry against the audit
baseline `b7edcbc`, and three executed python snippets (event-loop starvation demo with a `to_thread` control,
worst-case sleep count, autoflush check). No repo edits, no docker, no network.

## 1. Summary table

| id | claim (one line) | verdict | key evidence | conf |
|---|---|---|---|---|
| P1 | `app` runs a single uvicorn worker (Dockerfile:24, compose:29, no `--workers`) | STILL PRESENT (premise holds) | `Dockerfile:24` CMD uvicorn w/o `--workers`; `docker-compose.prod.yml:29` same; no `WEB_CONCURRENCY`/`--workers` anywhere in compose files, Dockerfile, `.env.example`, `src/main.py` | high |
| P2 | nginx `proxy_read_timeout 120s` | STILL PRESENT | `nginx/nginx.conf:145` (in `location /` of the `${DOMAIN}` server, line 128); also :124, :237, :302 | high |
| P3 | Neither endpoint touched by the PR stack / unchanged since baseline | CONFIRMED (no change) | every blame commit on the four code blocks is an ancestor of `b7edcbc` (b581c047, c10b3ddd, 38be9df0, 3d0717a9, aa38b04b, 5e796c68, a0f4b155); `git log -S to_thread` over admin.py/admin_provisioning.py/slack_provisioning.py = empty | high |
| P4 | Cited line numbers | STALE (symbols correct) | see §2.0 — `public.py:485-503` is now 479-541, `agent_page.py:487-513` is now 454-515, `admin.py:945-963/972-996` is now 964-989/991-1024, vote pattern `1038-1057` is now 1083-1099, etc. `slack_provisioning.py:124-152` and `:44-54` are unchanged | high |
| V5-1 | `waitlist_submit` does SELECT-then-INSERT with no `IntegrityError` catch | STILL PRESENT | `src/routers/public.py:516-519` SELECT, `:526` add, `:534` commit; AST: 0 try-blocks in the function; `IntegrityError` is imported (`:20`) but used only at `:1085` (vote) | high |
| V5-2 | `waitlist_signups.email` is UNIQUE | CONFIRMED | `src/models/access.py:43` `unique=True`; `alembic/versions/0010_access_gate_and_waitlist.py:76` `unique=True` | high |
| V5-3 | `review_proposal` guard is SELECT + `HTTPException(400)` then unguarded `db.add`/`commit` | STILL PRESENT | `src/routers/agent_page.py:487-494` SELECT+400, `:506` add, `:513` commit; AST: 0 try-blocks; `IntegrityError` imported `:14`, used only in `post_agent_message` (`:1018,:1022`) | high |
| V5-4 | `uq_proposal_reviews_decision_agent` exists | CONFIRMED (DB-side only) | `alembic/versions/0004_...py:84-88` on `(thread_decision_id, agent_id)`; the ORM model `ProposalReview` (`src/models/agent_registry.py:66-99`) declares NO `__table_args__` for it — constraint lives only in the migration; `tests/integration/test_db_contract.py:300-310` pins it | high |
| V5-5 | Per-IP limiter (10/3600 s) doesn't stop a double-click | CONFIRMED | `public.py:39` `SlidingWindowRateLimiter(max_events=10, window_seconds=3600)`; nginx `req_general` is 20r/s burst 40 (`nginx.conf:29,130`); no JS double-submit guard on the form (`templates/landing.html:620-634`) or the review form (`templates/agent/dashboard.html`) | high |
| V5-6 | Vote endpoint has the correct pattern | CONFIRMED | `public.py:1083-1099` try commit / except `IntegrityError` → rollback → re-select `scalar_one()` → update → commit | high |
| V5-7 | PI web-message writer: rollback + one retry then 409 | CONFIRMED | `agent_page.py:1015-1027` | high |
| V5-8 | The race surfaces as a 500 | CONFIRMED | no `exception_handler` anywhere in `src/`; `get_db` (`src/database.py:54-56`) rolls back and re-raises → Starlette 500 | high |
| V5-9 | DoD: a concurrent-insert test | STILL MISSING | only sequential duplicate tests exist (`tests/integration/test_agent_page.py:602-633`, `tests/integration/test_proposal_review.py:475-523`); no `asyncio.gather` against `/waitlist` or `/review` anywhere in tests/ | high |
| C2-1 | `admin_provision_slack` / `_callback` are `async def` awaiting `start_`/`complete_provisioning` | CONFIRMED | `src/routers/admin.py:964-989` (`:982 await start_provisioning`), `:991-1024` (`:1015 await complete_provisioning`) | high |
| C2-2 | `start_provisioning` calls sync `httpx.post` + `time.sleep` on the loop | STILL PRESENT (demonstrated) | `admin_provisioning.py:132-139,147,153` → `slack_provisioning.py:124-146`; **measured: 0 heartbeat ticks in a 2.07 s mocked rate-limit; `to_thread` control: 40 ticks** | high |
| C2-3 | Up to 5 retries × 60 s default ⇒ ~5 min freeze | CONFIRMED, and UNDERSTATED | measured 5 sleeps × 60 s = 300 s, plus up to 5 × 20 s httpx timeout = 400 s; `Retry-After` is **uncapped** (header 900 ⇒ 4500 s). `slack_web._call` caps at 30 s (fa143a6) but that cap is not applied here | high |
| C2-4 | `lookup_team_id` sync on the async path | STILL PRESENT | `slack_provisioning.py:44-54` (httpx, timeout 10) called from `admin_provisioning.py:184` | high |
| C2-5 | `exchange_code` sync on the async path | STILL PRESENT | `slack_provisioning.py:155-180` (httpx, timeout 15) called from `admin_provisioning.py:212` | high |
| C2-6 | (not in issue) `rotate_config_token` sync on the async path | NEW — MISSED BY ISSUE | `slack_provisioning.py:57-74` (httpx, timeout 15) called from `admin_provisioning.py:99` inside `_config_token` | high |
| C2-7 | `get_db` session + pooled connection held across the blocking call | STILL PRESENT | `_config_token` runs SELECTs (`:85-86,94`) → autobegin checks out a connection; `_create` at `:147` runs before `commit` at `:176`; `complete_provisioning` SELECTs `:193-205` precede `exchange_code :212`, commit `:231`; `get_db` yields the session for the whole handler (`database.py:47-58`); pool 5+10 | high |
| C2-8 | `asyncio.to_thread` precedents exist | CONFIRMED | `slack_web.py:274-300`, `grantbot.py:631`, `agent_page.py:319`; none in admin/provisioning; fa143a6 touched no provisioning file | high |
| C2-9 | nginx 120 s turns the freeze into a 504 for the admin | CONFIRMED (conditional) | needs ≥2 rate-limited rounds at the 60 s default; a single 60 s wait returns before the 120 s read timeout | high |
| C2-10 | DoD: a test that the handler does not block the loop | STILL MISSING | precedent/template exists at `tests/unit/test_slack_web.py:161-180` (thread-identity assertion) but covers only `slack_web`; `tests/unit/test_slack_provisioning.py:117` monkeypatches `time.sleep` to a no-op so it cannot observe the block; `test_admin_provisioning.py` covers only `_config_token` | high |

## 2. Per-item detail

### 2.0 Line-number drift (P4)
Issue cites (`b1d54da`) → current (`18ba52c`):
- `waitlist_submit` public.py:485-503 → **479-541**
- `review_proposal` agent_page.py:487-513 → **454-515**
- vote endpoint public.py:1038-1057 → **1017-1102** (the try/except is 1083-1099)
- PI web-message writer agent_page.py:1013-1027 → **949-1029** (the try/except is 1015-1027)
- `admin_provision_slack` admin.py:945-963 → **964-989**; callback :972-996 → **991-1024**
- `start_provisioning`/`complete_provisioning` admin_provisioning.py:124-186 → **124-187 / 190-233**
- `create_app` retry loop slack_provisioning.py:124-152 → **124-152 (unchanged)**; `lookup_team_id` :44-54 **(unchanged)**; `exchange_code` :154-172 → **155-180**
- `slack_web.py:274-299` → 274-300; `grantbot.py:624` → **631**; `agent_page.py:319` → 319
All symbol names are correct.

### 2.1 Premise (P1, P2, P3)
`Dockerfile:24`:
```
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
`docker-compose.prod.yml:29`:
```
    command: ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
`grep -rn "WEB_CONCURRENCY\|--workers\|workers=" docker-compose*.yml Dockerfile .env.example src/main.py` → no hits. Single process, single event loop.

`nginx/nginx.conf:128-145` (`location /` under `server_name ${DOMAIN}`): `proxy_connect_timeout 60s; proxy_send_timeout 120s; proxy_read_timeout 120s;`. The admin provisioning routes (`/admin/agents/...`) match this block.

Blame ancestry: every commit that last touched the four code blocks predates the audit baseline:
```
b581c047 2026-04-15  waitlist (landing page)           in b7edcbc: yes
c10b3ddd 2026-07-16  SEC-17 waitlist truncate/throttle  in b7edcbc: yes
38be9df0 2026-03-26  review_proposal                    in b7edcbc: yes
3d0717a9 / aa38b04b  review_proposal (email/delegate)   in b7edcbc: yes
5e796c68 2026-07-16  SEC-10 start_provisioning          in b7edcbc: yes
a0f4b155 2026-06-26  create_app retry loop              in b7edcbc: yes
```
`git log --oneline b7edcbc..HEAD -- <the 5 src files>` shows 25 commits, none of which changed these blocks (blame above). `git log -S to_thread -- src/services/admin_provisioning.py src/services/slack_provisioning.py src/routers/admin.py` → empty.

### 2.2 V5-1 `waitlist_submit` — STILL PRESENT
`src/routers/public.py:479-541` (excerpt, current lines):
```
488    if not _waitlist_limiter.allow(client_ip(request)):
489        raise HTTPException(status_code=429, detail="too many requests")
...
516    result = await db.execute(
517        select(WaitlistSignup).where(WaitlistSignup.email == email_clean)
518    )
519    existing = result.scalar_one_or_none()
520
521    if existing:
522        existing.name = name_clean or existing.name
...
525    else:
526        db.add(
527            WaitlistSignup(
528                email=email_clean,
...
533        )
534    await db.commit()
```
AST check (`scratchpad/24/ast_check.py`, run against the real module):
```
src/routers/public.py::waitlist_submit lines 479-541
  try-blocks: 0  except-types: []
  db.add at: [526]   commit at: [534]
```
`IntegrityError` is imported at `public.py:20` and used exactly once, at `:1085` inside `submit_proposal_vote`. Two concurrent first-time posts of the same email both see `existing is None`, both `add`, and the loser's `commit` raises `UniqueViolation` → unhandled → 500 (no `exception_handler` in `src/`; `get_db` rolls back and re-raises, `database.py:54-56`).

Mitigations looked for and not found: no client-side disable on the form (`templates/landing.html:620-634` is a plain `<form method="POST" action="/waitlist">` with a `<button type="submit">`), no nginx per-path limit beyond `req_general` 20r/s burst 40 (`nginx.conf:29,130`), no DB-side `ON CONFLICT`.

### 2.3 V5-3 `review_proposal` — STILL PRESENT (with a fix-shaping nuance)
`src/routers/agent_page.py:454-515` (excerpt):
```
487    existing = await db.execute(
488        select(ProposalReview).where(
489            ProposalReview.thread_decision_id == thread_decision_id,
490            ProposalReview.agent_id == agent.agent_id,
491        )
492    )
493    if existing.scalar_one_or_none():
494        raise HTTPException(status_code=400, detail="Already reviewed")
495
496    review = ProposalReview(
...
505    )
506    db.add(review)
507
508    # Record engagement and mark any outstanding email notification as responded
509    from src.services.email_notifications import mark_notification_responded, record_engagement
510    await record_engagement(current_user.id, db)
511    await mark_notification_responded(current_user.id, thread_decision_id, "review", db)
512
513    await db.commit()
```
AST: `try-blocks: 0`, `db.add at: [506]`, `commit at: [513]`. `IntegrityError` imported at `:14`, used only at `:1018/:1022` (`post_agent_message`).

**Nuance the issue does not mention (matters for the fix):** `record_engagement` and `mark_notification_responded` (`src/services/email_notifications.py:585-616`) each run `await db.execute(select(...))` on the same session. The session factory (`src/database.py:39-43`) does not set `autoflush`, and `Session.__init__`'s default is `autoflush=True` (verified: `scratchpad/24/autoflush.py`). So the pending `ProposalReview` INSERT is flushed at `:510`, and in a race the `IntegrityError` is raised from `record_engagement`'s `db.execute`, **not** from `await db.commit()` at `:513`. A fix that mirrors the vote endpoint by wrapping only `commit()` in `try/except IntegrityError` would NOT catch this race; the guard must span from `db.add` (or the helpers must be moved before `db.add`).

Unique constraint: `alembic/versions/0004_add_agent_registry_and_proposal_reviews.py:84-88`:
```
    op.create_unique_constraint(
        "uq_proposal_reviews_decision_agent",
        "proposal_reviews",
        ["thread_decision_id", "agent_id"],
    )
```
The ORM model (`src/models/agent_registry.py:66-99`) has no `__table_args__`/`UniqueConstraint` — the constraint exists only in the migration. Not a defect for prod or the test suite (both run alembic; `tests/integration/test_db_contract.py:300-310` asserts the violation), but a `metadata.create_all`-based fixture would silently lack it.

### 2.4 V5-6 / V5-7 the "correct pattern" precedents — CONFIRMED
`public.py:1083-1099`:
```
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race on the unique (decision, token) constraint — fetch & update.
        await db.rollback()
        vote_obj = (await db.execute(select(ProposalVote).where(...))).scalar_one()
        vote_obj.vote = payload.vote
        ...
        await db.commit()
```
`agent_page.py:1015-1027`:
```
    try:
        await _write()
    except IntegrityError:
        await db.rollback()
        try:
            await _write()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Message could not be saved ...")
```
Issue wording nit: the "rollback + one retry then 409" description applies only to the message writer; the vote endpoint does rollback + re-select + update (no 409). Both are valid templates. Note that in the vote endpoint no query runs between `db.add` (`:1081`) and `commit` (`:1084`), so wrapping `commit` alone is sufficient *there* — which is exactly why it is not sufficient for `review_proposal` (see 2.3).

### 2.5 V5-5 limiter and edge — CONFIRMED
`public.py:39`: `_waitlist_limiter = SlidingWindowRateLimiter(max_events=10, window_seconds=3600)`; `src/services/rate_limit.py:57-68` allows the first 10 events per key. A double-click is 2 events. nginx `limit_req zone=req_general burst=40 nodelay` (`nginx.conf:130`, zone `rate=20r/s` at `:29`) likewise passes 2 near-simultaneous requests.

### 2.6 V5-9 tests — MISSING
- `tests/characterization/test_public_routes.py:26-58`: valid email → 200 (`:26`), invalid → 400, missing → 422, oversized fields truncated. No duplicate or concurrent submission.
- `tests/integration/test_agent_page.py:602-633` `test_a_pi_review_is_recorded_and_cannot_be_submitted_twice`: two **sequential** posts; second asserts 400. This exercises the SELECT guard, not the race; it would pass against the buggy code (and does).
- `tests/integration/test_proposal_review.py:475-523` `test_a_decided_review_cannot_be_re_decided`: same, sequential, asserts 400 + "Already reviewed" + single row.
- `tests/integration/test_db_contract.py:300-310`: asserts the constraint raises `IntegrityError` at the DB level — proves the 500 path exists, does not exercise the handler.
- `grep -rn 'asyncio.gather' tests/unit tests/integration tests/characterization` hits only worker claim tests, simulation logic, and live-run tests. Nothing gathers against `/waitlist` or `/review`.

### 2.7 C2-1..C2-7 the provisioning path — STILL PRESENT (mechanically demonstrated)
Handlers, `src/routers/admin.py`:
```
964 @router.post("/agents/{agent_id}/slack/provision")
965 async def admin_provision_slack(... db: AsyncSession = Depends(get_db), ...):
...
982         oauth_url = await start_provisioning(db, agent)

991 @router.get("/agents/slack/callback")
992 async def admin_provision_slack_callback(... db: AsyncSession = Depends(get_db), ...):
...
1015        agent = await complete_provisioning(db, state, code)
```
Service, `src/services/admin_provisioning.py` — every outbound call classified:
| line | call | sync/async | httpx timeout | sleeps? |
|---|---|---|---|---|
| 99 | `rotate_config_token(refresh)` (inside `_config_token`) | **sync** | 15 s | no |
| 147 / 153 | `_create(token)` → `create_app(...)` | **sync** | 20 s × ≤5 | **`time.sleep(wait)` × ≤5** |
| 184 | `lookup_team_id(team_token)` | **sync** | 10 s | no |
| 212 | `exchange_code(...)` (in `complete_provisioning`) | **sync** | 15 s | no |
| 85,86,94,162,176,182 | `_kv_get`, `delete(...)`, `db.commit()`, `get_any_bot_token(db)` | async (DB) | — | — |

`src/services/slack_provisioning.py:124-152` (the retry loop, unchanged since a0f4b155):
```
124    for attempt in range(max_rate_limit_retries):          # default 5 (line 83)
125        resp = httpx.post(
126            f"{SLACK_API}/apps.manifest.create",
...
129            timeout=20,
130        )
131        data = resp.json()
132        if data.get("ok"):
...
143        if data.get("error") == "ratelimited":
144            wait = int(data.get("retry_after", 0) or resp.headers.get("Retry-After", 60))
145            logger.warning("apps.manifest.create rate limited — waiting %ds before retry", wait)
146            time.sleep(wait)
147        else:
...
150    raise RuntimeError(
151        f"apps.manifest.create: still rate-limited after {max_rate_limit_retries} retries"
152    )
```
Note the loop sleeps on **all five** iterations (including the last, before raising), so worst case is 5 sleeps + 5 HTTP round-trips, not 4.

**Executed — worst-case block (`scratchpad/24/worst_case.py`, `time.sleep` and `httpx.post` monkeypatched to count):**
```
[no retry_after, no header (defaults)] posts=5 sleeps=[60, 60, 60, 60, 60] total_sleep=300s (+ up to 5x20s httpx timeout = 400s max)
[Retry-After header = 900]             posts=5 sleeps=[900, 900, 900, 900, 900] total_sleep=4500s (+ ... = 4600s max)
[retry_after body = 30]                posts=5 sleeps=[30, 30, 30, 30, 30] total_sleep=150s (+ ... = 250s max)
```
The issue's "~5 minutes" is the default-path floor; there is **no cap** on Slack's `Retry-After` here, whereas `slack_web._call` caps at `_MAX_RETRY_AFTER` (30 s) since fa143a6 (`slack_web.py:105-120`). The raise message "still rate-limited after 5 retries" does not match any `_AUTH_ERRORS` slug (`admin_provisioning.py:40-46`), so no second `_create` round follows.

**Executed — event-loop starvation (`scratchpad/24/loop_block2.py`; `httpx.post` mocked: 2 × `ratelimited retry_after=1` then success; `_config_token`, `get_any_bot_token`, DB all faked; 50 ms heartbeat task on the same loop):**
```
[CURRENT: await start_provisioning()]           call took 2.07s; heartbeat ticks DURING call: 0 (~41 if loop free); largest gap between ticks: 2.12s
[CONTROL: await asyncio.to_thread(create_app)]  call took 2.00s; heartbeat ticks DURING call: 40 (~40 if loop free); largest gap between ticks: 0.05s
```
The current code freezes the loop for the whole duration; the `to_thread` pattern the issue prescribes leaves it free.

Session/connection hold (C2-7): `get_db` (`database.py:47-58`) yields one `AsyncSession` for the whole handler. In `start_provisioning`, `_config_token` executes SELECTs at `:85-86` before `_create` at `:147`; SQLAlchemy autobegins a transaction on first execute and holds the pooled connection until `commit` at `:176` — i.e. across the blocking `create_app`. In `complete_provisioning`, SELECTs at `:193-205` precede the blocking `exchange_code` at `:212`; commit at `:231`. Pool: `pool_size=5, max_overflow=10` (`database.py:20-21`). While the loop is frozen this is moot (nothing else runs), but it becomes the live problem the moment the blocking call is moved to a thread without committing first — the issue's second fix bullet is correct.

### 2.8 C2-8 precedents — CONFIRMED
`grep -rn "to_thread\|run_in_executor\|run_in_threadpool" src/`:
```
src/services/slack_web.py:274,282,287,292,299   (five *_async wrappers)
src/agent/grantbot.py:631                        (_ensure_channel_membership)
src/routers/agent_page.py:319                    (_resolve_delegate_names)
```
Zero hits in `admin.py`, `admin_provisioning.py`, `slack_provisioning.py`. Commit fa143a6 ("keep the Slack boundary off the event loop") added the wrappers and touched `grantbot.py`, `agent_page.py`, `slack_web.py`, `main.py`, `invite.py`, `email_inbound.py` — not the provisioning path. Its message explicitly enumerates "seven call sites" that reach slack_sdk; the provisioning path uses raw `httpx`, so it was outside that audit's scope and remains unwrapped. (`tests/unit/test_slack_boundary.py` only polices `slack_sdk` imports, so httpx-based blocking calls are invisible to it.)

### 2.9 C2-9 the 504 — CONFIRMED, conditional
`proxy_read_timeout 120s` (`nginx.conf:145`). A single rate-limited round at the 60 s default returns in ~60-80 s (< 120 s) and the admin gets the redirect; two or more rounds exceed 120 s and nginx returns 504 to the admin while the app keeps sleeping. Every other request that arrives during the freeze is accepted by the kernel backlog and then also 504s once it has waited 120 s. Coupling with issue #27 I5 as stated is correct.

### 2.10 C2-10 tests — MISSING for provisioning
- `tests/unit/test_slack_web.py:161-180` `test_the_async_wrapper_runs_the_blocking_call_off_the_event_loop`: asserts `threading.get_ident()` inside the mocked Slack call differs from the loop thread. This is the exact shape the DoD asks for, but it targets `slack_web.lookup_user_by_email_async` only. `:183-189` `test_every_sync_entry_point_has_an_async_wrapper` enumerates only `slack_web` names.
- `tests/unit/test_slack_provisioning.py:114-138` `test_create_app_retries_only_on_rate_limit`: `monkeypatch.setattr(time, "sleep", lambda _s: None)` — it deliberately zeroes the sleep and calls `create_app` synchronously, so it can neither observe nor fail on the loop block.
- `tests/unit/test_admin_provisioning.py` (3 tests): only `_config_token` caching/rotation with a `_FakeDB`; `start_provisioning`/`complete_provisioning` are never called.
- `tests/integration/test_slack_provision_live.py:44` calls `lookup_team_id` live (network tier), no loop assertion.
- Both DB-free files pass on this tree: `pytest tests/unit/test_admin_provisioning.py tests/unit/test_slack_web.py` → `16 passed in 0.47s`.

## 3. Counts
**Defect sub-claims:** 10 still present (V5-1, V5-3, V5-9, C2-2, C2-3, C2-4, C2-5, C2-7, C2-10, plus premise P1/P2 hold), 0 fixed, 0 partial, 0 changed, 0 not reproducible.
**Supporting claims:** 9 confirmed as stated (V5-2, V5-4, V5-5, V5-6, V5-7, V5-8, C2-1, C2-8, C2-9). 1 stale (P4 line numbers). 1 new item the issue missed (C2-6 `rotate_config_token`).

Contradictions of the issue text: none on substance. Understatements: C2-3 worst case is 400 s on defaults and unbounded via `Retry-After`; V5-3's race raises at autoflush inside `record_engagement` (`agent_page.py:510`), not at `commit`, so a commit-only guard would not fix it.

## 4. What I could not verify and why
- The actual 500 under two concurrent POSTs (V5) was not reproduced end-to-end: doing so needs a live Postgres + the FastAPI test client, which the PREAMBLE forbids (no Docker). The verdict rests on the AST proof of zero exception handlers around `add`/`commit`, the confirmed unique constraints, and `test_db_contract.py:300` proving the DB raises. Confidence high.
- Whether Slack actually returns `Retry-After` values > 60 s for `apps.manifest.create` in practice (no network allowed). The code path is uncapped regardless.
- The C2 freeze was demonstrated at the service layer (`start_provisioning`) with faked DB/tokens, not through the ASGI handler; the handler is a thin `await start_provisioning(...)` (`admin.py:982`) with no threadpool indirection, so the result transfers.

Scripts and raw outputs: `/tmp/claude-1000/-home-a-scripps-coPI-science/c8a1ec5c-25b0-4282-b6cd-c2567cafee26/scratchpad/24/{ast_check.py,loop_block2.py,worst_case.py,autoflush.py}`.
