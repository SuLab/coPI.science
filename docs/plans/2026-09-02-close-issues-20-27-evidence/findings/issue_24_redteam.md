# Issue #24 — red-team pass on the first agent's report

Tree: `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c, clean. Every row re-derived by fresh grep/read. Two
experiments re-run from scratch with `.venv-test/bin/python` (`scratchpad/rt24/starve.py` — hard-blocks
`socket.connect`, mocks `httpx.post`, keeps a *real but shortened* blocking `time.sleep`; `scratchpad/rt24/autoflush.py`).

## 1. Table

| id | first-agent verdict | red-team | one-line reason | evidence |
|---|---|---|---|---|
| P1 | STILL PRESENT (single worker) | UPHELD | `Dockerfile:24` and `docker-compose.prod.yml:29` both `uvicorn src.main:app --host --port` only; repo-wide grep for `--workers`/`WEB_CONCURRENCY`/`gunicorn` hits only `scripts/wipe_slack.py` (a ThreadPoolExecutor, unrelated) | grep |
| P2 | STILL PRESENT | UPHELD | `nginx/nginx.conf:145` `proxy_read_timeout 120s` inside `location /` (:128) of the `${DOMAIN}` TLS server (:71-75); no separate `location /admin` | grep |
| P3 | CONFIRMED unchanged | UPHELD | re-ran blame on `public.py:516-534` (b581c047, c10b3ddd) and `agent_page.py:487-513` (38be9df0, 3d0717a9, aa38b04b): all `merge-base --is-ancestor b7edcbc`; `git log -S to_thread` on the three provisioning files → empty; 2 commits touched them since baseline, neither adds offloading | git |
| P4 | STALE line numbers | UPHELD | current ranges checked: `public.py:479-541`, `agent_page.py:454-515`, `admin.py:964-989/991-1024`, `slack_provisioning.py` `lookup_team_id` :42-52, `rotate_config_token` :53-72, `create_app` loop :124-152, `exchange_code` :153-180 | sed |
| V5-1 | STILL PRESENT | UPHELD | `public.py:516-519` SELECT, `:526` add, `:534` commit, no try; `IntegrityError` imported `:20`, used only `:1085` | read |
| V5-2 | CONFIRMED | UPHELD | `src/models/access.py:43` `unique=True` | read |
| V5-3 | STILL PRESENT | UPHELD | `agent_page.py:487-494` SELECT + 400, `:506` add, `:510-511` two more SELECTs, `:513` commit, no try | read |
| V5-4 | CONFIRMED (DB-side only) | QUALIFIED | constraint is migration-only (`alembic/0004:84-88`) — correct — but the report says the model "declares NO `__table_args__`" and spans `66-99`; it in fact declares `__table_args__` at `agent_registry.py:105-108` as a comment-only dict (`{"comment": "unique constraint ... added in migration"}`), and the class runs to :108. Substance right, wording wrong; and it is deliberate, not an oversight | read |
| V5-5 | CONFIRMED | UPHELD | `public.py:39` limiter 10/3600; `nginx.conf:29,130` 20r/s burst 40; `templates/landing.html:620-634` plain form; `templates/agent/dashboard.html:185` plain form; `templates/base.html` and `static/js/markdown.js` contain no submit-disable/preventDefault guard | grep |
| V5-6 | CONFIRMED | UPHELD | `public.py:1083-1099` | read |
| V5-7 | CONFIRMED | UPHELD | `agent_page.py:1016-1027` | read |
| V5-8 | CONFIRMED (500) | UPHELD | no `exception_handler` anywhere in `src/`; `AgentBadgeMiddleware.dispatch` returns `await call_next(request)` outside its try (`main.py:104`); `get_db` rolls back and re-raises (`database.py:52-58`) → Starlette 500 | grep/read |
| V5-9 | MISSING test | UPHELD | no test file containing `gather` also mentions `waitlist` or `/review`; `test_db_contract.py:300-310` proves the DB raises, not the handler | grep |
| C2-1 | CONFIRMED | UPHELD (+reachability) | `admin.py:964-989` awaits `start_provisioning` (:982); `:991-1024` awaits `complete_provisioning` (:1015). Both routes carry `Depends(get_admin_user)` (`dependencies.py:150-155`, 403 otherwise) — only an admin can trigger the freeze, but it stalls every user | read |
| C2-2 | STILL PRESENT (demonstrated) | UPHELD — independently reproduced | my run: current path **1 heartbeat tick in 1.56 s (~31 if free), `httpx.post` ran on the loop thread**; `to_thread` control **30 ticks in 1.50 s, ran off-thread** | §2 output |
| C2-3 | CONFIRMED, understated | QUALIFIED | reproduced: defaults → 5 sleeps × 60 = 300 s; `Retry-After: 900` → 4500 s (uncapped, unlike `slack_web._MAX_RETRY_AFTER=30`); body `retry_after=30` → 150 s; the loop **does** sleep on the 5th iteration before raising. But "+ up to 5 × 20 s httpx timeout = 400 s" is mis-framed: an httpx timeout *raises* out of `create_app` (no try around `httpx.post`), ending the sequence, so timeouts shorten the freeze; 400 s needs five slow-but-answered (~20 s) rate-limited responses | §2 output |
| C2-4 | STILL PRESENT | UPHELD | `slack_provisioning.py:42-52` sync `httpx.post` (10 s); called `admin_provisioning.py:184` | read |
| C2-5 | STILL PRESENT | UPHELD | `slack_provisioning.py:153-180` sync (15 s); called `admin_provisioning.py:212` | read |
| C2-6 | NEW (rotate_config_token) | UPHELD — verified | `slack_provisioning.py:53-72` sync `httpx.post` (15 s); called from `_config_token` `admin_provisioning.py:99`; reached whenever the KV-cached token is absent/within 120 s of expiry and a refresh token exists (prod has `SLACK_CONFIG_REFRESH_TOKEN` per CLAUDE.md) — i.e. first use and every token expiry, on the async handler path | read |
| C2-7 | STILL PRESENT | UPHELD | `_config_token` runs `_kv_get` SELECTs (:85-86) → autobegin → connection held through `_create` (:147/:153) until `commit` (:176); `complete_provisioning` SELECTs (:193-205) precede `exchange_code` (:212), commit (:231); pool 5+10 (`database.py`) | read |
| C2-8 | CONFIRMED | UPHELD | `to_thread` at `slack_web.py:274-300`, `grantbot.py:631`, `agent_page.py:319`; none in provisioning files | grep |
| C2-9 | CONFIRMED (conditional) | UPHELD (+note) | arithmetic: one 60 s round + two ≤20 s calls ≤ 100 s < 120 s; two rounds ≥ 120 s → 504. Note also a *single* round 504s if the uncapped `Retry-After` ≥ 120 | — |
| C2-10 | MISSING test | UPHELD | `test_slack_provisioning.py:117` stubs `time.sleep` to no-op; `test_admin_provisioning.py` has 3 tests, all `_config_token`; `test_slack_web.py:161` thread-identity test covers slack_web only | read |

New claims by the first agent, each re-verified:
- **Retry-After uncapped, worst case 4500 s; final iteration sleeps** — reproduced (see C2-3).
- **`rotate_config_token` is a fourth sync call on the async path** — confirmed (C2-6).
- **autoflush makes the IntegrityError surface at `record_engagement`'s `db.execute`, not at `commit`** — reproduced:
  `async_sessionmaker(class_=AsyncSession, expire_on_commit=False).kw` → `{'autoflush': True, 'expire_on_commit': False}`
  (exactly `src/database.py:39-43`'s kwargs); a sync-Session model of the race raises `IntegrityError` at `execute()`
  (autoflush), never reaching `commit()`. So a commit-only `try/except` (the vote-endpoint template) would NOT fix
  `review_proposal`; the guard must start at `db.add` (:506) or the two helper calls must move above it.
- **`ProposalReview` has no `UniqueConstraint`** — confirmed in substance, wording corrected (V5-4).
- **nginx 504 needs ≥2 rounds** — confirmed for defaults (C2-9).

Mitigation hunt (all negative): no global `IntegrityError`→4xx handler; no `ON CONFLICT`/upsert in `waitlist_submit`
(plain `db.add`); no client-side double-submit guard in `landing.html`, `dashboard.html`, `base.html`, `static/js/markdown.js`;
no second uvicorn worker / gunicorn / `WEB_CONCURRENCY` in Dockerfile, compose files, pyproject or `.env*`; the
provisioning routes are admin-only (`get_admin_user`), which limits *who* can trigger C2 but not *who* is affected.

## 2. Detail for QUALIFIED rows and re-run experiments

**C2-2 starvation experiment (my run, `rt24/starve.py`).** `httpx.post` mocked to answer `ratelimited retry_after=1`
three times then `ok`; `slack_provisioning.time.sleep` replaced by a **real** 0.5 s blocking sleep; `_config_token`,
`lookup_team_id`, `get_any_bot_token`, `get_settings` and the DB faked; a 50 ms heartbeat task on the same loop:
```
CURRENT: await start_provisioning(db, agent)     took 1.56s  heartbeat ticks during call:   1 (~31 if loop free)
   posts=4 ran on loop thread=True
CONTROL: await asyncio.to_thread(create_app, ...) took 1.50s  heartbeat ticks during call:  30 (~30 if loop free)
   posts=4 ran on loop thread=False
```
The single tick in the current path fires before the first `httpx.post`; nothing else on the loop runs until
`start_provisioning` returns. (The script's "max gap" column is an artefact of how the heartbeat is stopped — ignore it.)

**C2-3 worst-case accounting (same script, `time.sleep` replaced by a counter, `httpx.post` always rate-limited):**
```
defaults (no retry_after, no header)     posts=5 sleeps=[60, 60, 60, 60, 60] total=300s -> apps.manifest.create: still rate-limited after 5 retries
Retry-After header 900                   posts=5 sleeps=[900, 900, 900, 900, 900] total=4500s -> ...
body retry_after=30                      posts=5 sleeps=[30, 30, 30, 30, 30] total=150s -> ...
HTTP-date Retry-After via start_provisioning -> ProvisioningError: Could not create the Slack app: invalid literal for int() with base 10: 'Wed, 21 Oct 2015 07:28:00 GMT'
```
Qualifications: (a) `slack_provisioning.py:125-130` has no try around `httpx.post`, so a 20 s `ReadTimeout` propagates
out of `create_app` on that attempt → `start_provisioning`'s `except Exception` → `ProvisioningError` → redirect. Timeouts
therefore *terminate* the freeze; the 400 s figure requires five slow-but-successful rate-limited answers, not five
timeouts. (b) The `int()` on `Retry-After` (`:144`) has the same HTTP-date fragility as `slack_client.py:331` (issue #23
V7e) but here it is *caught* — the handler's `except ProvisioningError` sees it as a normal failure — so it is not an
uncaught-escape defect, just a spurious "could not create" for the admin. (c) The only way to get a second `create_app`
round is an `_AUTH_ERRORS` slug in the first exception (`admin_provisioning.py:148-153`); the rate-limit exhaustion
message contains none, so 5 attempts is the ceiling per request — the first agent said this too.

**V5-4 wording.** `src/models/agent_registry.py:105-108`:
```
    __table_args__ = (
        # Each agent can only review a thread decision once
        {"comment": "unique constraint on (thread_decision_id, agent_id) added in migration"},
    )
```
So the model does declare `__table_args__`, just without a `UniqueConstraint`, and documents the choice. Not a defect
(tests migrate via alembic — `tests/conftest.py:5,63`); the first agent's "declares NO `__table_args__`" and "`:66-99`"
are both slightly off.

**V5-3 autoflush (reproduced, `rt24/autoflush.py`).**
```
Session.__init__ autoflush default: True
async_sessionmaker kw as configured in src/database.py: {'autoflush': True, 'expire_on_commit': False}
IntegrityError raised at: execute() (autoflush)
```
Modelled the race with a sync Session (identical flush semantics): winner committed; loser's guard SELECT passed,
`add()`, then an unrelated `execute(select(...))` — the flush fires there and raises. In `review_proposal` that is
`record_engagement`'s `db.execute` (`email_notifications.py:585`) at `agent_page.py:510`, three lines before `commit`.

## 3. Mis-cites by the first agent

- V5-4: "`ProposalReview` (`src/models/agent_registry.py:66-99`) declares NO `__table_args__`" — the class extends to
  :108 and declares a comment-only `__table_args__` at :105-108.
- C2-3: "plus up to 5 × 20 s httpx timeout" — timeouts raise and end the loop; the 20 s/attempt is a slow-response bound.
- Otherwise every cited range I checked resolves to the quoted code (`admin.py:964-989/991-1024`,
  `admin_provisioning.py:85-99,147-153,176,184,193-231`, `slack_provisioning.py:124-152`, `public.py:479-541,1083-1099`,
  `agent_page.py:454-515,1015-1027`, `database.py:39-58`, `nginx.conf:29,128-145`, `Dockerfile:24`, compose `:29`,
  `test_slack_provisioning.py:117`, `test_slack_web.py:161`). Minor: `lookup_team_id` starts at :42 (report: :44),
  `exchange_code` at :153 (report: :155) — same code.

## 4. Counts

**23 rows: Upheld 21 · Overturned 0 · Qualified 2 (V5-4 wording/range; C2-3 timeout framing) · Unverifiable 0.**
The issue's two defects (V5 race → 500, C2 loop freeze) both stand at HEAD; the first agent's two sharpenings that
matter for the fix — the uncapped `Retry-After` and the autoflush placement of the `IntegrityError` — are both real.
