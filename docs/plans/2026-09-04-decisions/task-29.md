# Task 29 — the health probe returns 503 on a healthy database

## Ruling

**The plan's Step 3 — `pool_pre_ping=True` — was measured and rejected. It reopens phase-8
audit C2, the Critical this same task has to assert closed.** Shipped instead: the probe
retries once, and only when SQLAlchemy itself reports the connection was invalidated
(`DBAPIError.connection_invalidated`). The engine's kwargs are unchanged.

Four options, all built and measured in one `docker pause` window against the disposable
production copy (`copi-prodtest-db`, `127.0.0.1:55434`, db `copi_verify`), each as its own
uvicorn served from its own `git archive` export:

| | reaped pooled conn | probe vs frozen Postgres | first probe after the outage |
|---|---|---|---|
| **A** HEAD (`pool_pre_ping=False`) | **503** ← the defect | 503 in 5.004 s | 503, then 200 |
| **B** `pool_pre_ping=True` (Step 3) | 200 | **no response at all** (>15 s, >25 s) | 200 |
| **C** B + the whole `async with` bounded | 200 | 503 in 5.004 s | 503 (pool timeout), then 200 |
| **D** HEAD + retry on `connection_invalidated` | 200 | 503 in 5.005 s | **200** |

**Why B fails.** SQLAlchemy runs the pre-ping inside `engine.connect()`, which sits *outside*
the probe's `asyncio.wait_for`. asyncpg's own `command_timeout` does not rescue it: the driver's
timer fires, but its cancel handshake opens a *second* connection to the same frozen server and
blocks there. So the request never returns — which is C2 verbatim ("`/api/health` never returned
… three requests each hung 40 s").

**Why not C.** It works, and it is what the C2 audit itself recommends, but the cancellation
lands *inside* the pool checkout, orphaning the one pooled connection: for ~5 s after the
database returns, every probe still 503s with `QueuePool limit of size 1 … timeout 3.00`. It
also restructures the probe body for a defect that does not need it.

**Why D.** Smallest change that is bounded by construction. A reaped socket raises a disconnect
error; SQLAlchemy has already invalidated and discarded that connection by the time the handler
sees it, so one retry gets a fresh one. A real outage raises `TimeoutError` or a connect failure,
neither of which sets `connection_invalidated`, so it is never retried and the answer still lands
inside `HEALTH_PROBE_TIMEOUT_SECONDS`. D was the only variant that answered 200 on the first probe
after the outage.

## Evidence

**The defect, red first** (`.venv-test/bin/python -m pytest tests/integration/test_health_route.py
-q -p no:cacheprovider`, run from a `git archive HEAD` export with the new test copied in):

```
E   AssertionError: the probe reported a healthy database unavailable because its own
    pooled connection had been reaped server-side
E   assert 503 == 200
WARNING src.main: Health check DB probe failed: (…asyncpg.InterfaceError)
        <class 'asyncpg.exceptions._base.InterfaceError'>: connection is closed
[SQL: SELECT 1]
```

After the change: `10 passed` (`tests/integration/test_health_route.py`,
`tests/unit/test_health_route_unit.py`, `tests/unit/test_agent_badge_middleware.py`).

**Reaped-connection behaviour, out of process** (`pg_terminate_backend` on every `copi_verify`
backend, then one probe each):

```
20:14:43.650  A HEAD       after reap -> 503 0.0039     20:14:43.827  A next -> 200
20:14:43.707  C ping+bound after reap -> 200 0.0445     20:14:43.844  C next -> 200
20:14:43.767  D retry      after reap -> 200 0.0466     20:14:43.860  D next -> 200
```

**phase-8 C2, re-run** (`docker pause copi-prodtest-db`; unpause in a bash `trap`, container
verified `running` after every run):

```
20:11:18.184 PAUSE                      20:14:43.862 PAUSE
20:11:23.259 C -> 503  5.0045           20:14:48.943 A -> 503  5.0042
20:11:23.261 A -> 503  5.0063           20:14:48.945 C -> 503  5.0043
20:11:33.258 B -> 000 15.0023  (hang)   20:14:48.946 D -> 503  5.0046
round2: A 503 0.0035 | C 503 3.0034 | B 503 3.0039     20:14:48.948 UNPAUSE
conc x3 each: all 503 in ~3.00 s        t+5s:  A 503 0.0038 | C 503 3.0037 | D 200 0.052
(one A at 6.00 s)                       t+10s..t+60s: A, C, D all 200
```
An earlier two-way run measured B at `000 25.0028` in the same experiment.

**Probe cost on a healthy database** (6 probes each, full HTTP round trip via curl):
HEAD/D **3.4–4.4 ms**, `pool_pre_ping=True` **4.6–6.0 ms** — pre-ping costs ~1.3 ms, which is
indeed "comfortably inside `HEALTH_PROBE_TIMEOUT_SECONDS = 5.0`". Cost was never the reason to
reject it. D adds nothing on the healthy path: it runs only after a failure.

**Re-measure with:**
`/tmp/.../scratchpad/task29/c2_acd.sh` (scratch; A/C/D exports + pause + 60 s recovery poll).
Ports 8791/8793/8794, DSN `postgresql+asyncpg://copi:copi@127.0.0.1:55434/copi_verify`.
Never point it at production.

**Lint, measured on clean `git archive` exports** (other agents have uncommitted edits in this
checkout): ruff `src` **250 → 250**, mypy `src` **145 → 145**, ruff test-suite targets clean both
ways. D33: AST walk of every string constant in `src/main.py`, HEAD vs mine — 45 → 46, the only
two differences being the new `logger.info` format string and the corrected `get_health_engine`
docstring. No model-facing string.

## Addendum — the third `rating != -1` reader (#20 blocker 5), folded in by the coordinator

`9505554` (Task 16) fixed `src/routers/admin.py:656` and `:865`; `src/main.py:182`, the nav-badge
count in `AgentBadgeMiddleware`, was the remaining one and is in this task's file. Changed to
`ProposalReview.rating.notin_((-1, 0))` on Task 16's measurements (rating distribution on
`copi_verify`: 0 → 233, −1 → 0; 12 of 53 active agents over-counted, wiseman by 89), not re-derived.

Red first, `tests/integration/test_agent_badge_count.py` (new file):

```
>       assert await _badge_count(db_session, monkeypatch, pi) == 2
E       AssertionError: a reopen marker was counted as a completed review, so the badge
        under-reports the PI's outstanding proposals
E       assert 1 == 2
```
Two controls in the same file passed both before and after — a real `rating=3` counts (badge 1)
and the engine's `rating=-1` does not (badge 2) — so the failing 1 is the defect, not a harness
that reads 0 for everything.

**Seam, since there was none:** the badge count is written to `request.state.agent_badge_count`
and rendered only by the nav template, and the middleware calls `get_session_factory()` directly
rather than the injected `get_db`. The test therefore builds its own `create_app()`, mounts a
one-line probe route that returns the value, and repoints the factory at the rolled-back
`db_session`. Nothing is committed and no fixture file was edited. This is a **new file added to
the task's commit path list** — the alternative was editing `tests/unit/test_agent_badge_middleware.py`,
which belongs to another task.

## Consequence a closing comment must state

1. **phase-8 audit C2 is CLOSED, measured, not inferred.** `5090322`'s dedicated engine with
   `command_timeout=4.0` / connect `timeout=3.0` does close it: against a paused Postgres every
   probe answered — 503 in 5.004 s first, 3.00 s (`pool_timeout`) while the first probe's
   connection is still held, 0.004 s once the pooled socket is known dead. Nothing hung; the
   container goes unhealthy by 503, not by probe timeout. C2's accumulation claim ("every probe
   leaves a hung request holding a pool connection … eight minutes exhausts the pool") no longer
   applies to the request pool at all: the probe has its own engine capped at one connection.
2. **Residual, stated rather than hidden.** During an outage the probe's single connection is
   held by an orphaned greenlet, so probes 2..n 503 on `pool_timeout` instead of on the database —
   correct verdict, different reason — and one probe after the database returns may still 503
   (measured: A and C did; the shipped D did not). No unbounded wait remains in the probe.
3. **`pool_pre_ping` must stay off on the *probe* engine** (`make_engine`'s app pool keeps its own,
   #25 P3.1 — untouched). Anyone re-adding it must first re-run the pause experiment above: it is
   the difference between a 5 s 503 and a request that never returns.
4. Nothing here changes #27 I2's `Fix:` clause deliverables (the `SELECT 1` and the migrate gate);
   this is repair of a regression introduced by `5090322` on this branch, not new scope.
5. **#20 blocker 5 is now closed in all three readers.** `9505554` fixed the two in
   `src/routers/admin.py`; `src/main.py:182` (the nav badge) is fixed here, with the first test in
   the suite that asserts a badge *count* rather than which tables it queries.
