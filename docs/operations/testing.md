# Testing

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

Run `./scripts/ci.sh` before committing — alembic sanity (single head, no
duplicate revision ids), an upgrade→downgrade→upgrade round trip against a
throwaway Postgres it creates and destroys itself, `ruff check` on the test
suite (zero findings) plus a ratcheted ceiling on `src/`, then the full pytest
run with a branch-coverage floor. This is exactly what the `pre-push` hook
runs, and it is the whole gate: there is no server-side CI.

The round trip's throwaway Postgres is named `copi-ci-migcheck-<pid>` on a free
127.0.0.1 port (override with `MIGCHECK_PORT`), so two runs no longer collide on it.
A container leaked by an interrupted run is removed by name at that run's exit.

**The supported way to run pytest alone is on the host, not inside a
container:**

```bash
.venv-test/bin/python -m pytest tests/ -v
```

(If `.venv-test` doesn't exist yet: `uv venv .venv-test && uv pip install
--python .venv-test/bin/python -e '.[dev]'`.) The host has a Docker socket, so
with `TEST_DATABASE_URL` unset, `tests/conftest.py` spins its own ephemeral
Postgres via testcontainers and migrates it with the real alembic chain — no
container, no manual database, no env var needed. This is exactly what
`scripts/ci.sh` runs.

> ### ⚠️ Two host/sshfs hazards that have each cost multiple sessions real time.
>
> 1. **Never run `pip install` against `.venv-test` from a client mounting this
>    repo over sshfs.** It corrupts the venv's console-script shebangs — they
>    get rewritten to the client's own interpreter path, which does not exist
>    on the host — and every DB-backed test then fails with a plain
>    `FileNotFoundError` that has nothing obviously to do with the real cause.
>    Run `pip install`/`uv pip install` against `.venv-test` on the host itself.
> 2. **Running pytest through an sshfs-mounted checkout can be dramatically
>    slower than on the host's local disk** — measured as much as 100-400x
>    slower in practice, from FUSE round-trips on every file read. If a run
>    that normally takes minutes appears to hang, check whether you're on an
>    sshfs mount before assuming a real regression.

> ### ⚠️ The suite does not necessarily run production's Anthropic SDK.
>
> `pyproject.toml` pins only `anthropic>=0.26.0`, and the two environments have
> resolved different versions: the deployed images (web, worker and agent) have
> **1.8.0**, while `.venv-test` has **0.120.2** (measured 2026-09-24; the agent
> image was on 1.0.0 when this note was first written, 2026-08-21; re-measured
> 2026-09-29: 1.9.0 in all three images, still 0.120.2 in `.venv-test`). So a test that passes
> here says nothing certain about SDK behaviour in the container. **Do not
> "fix" this by tightening the pin** — dependency churn on a live deployment is
> the riskier move; just know the skew is there when a failure smells like the
> client library.
>
> The one SDK behaviour this has already cost us is the non-streaming
> `max_tokens` ceiling: `BaseClient._calculate_nonstreaming_timeout` refuses any
> non-streaming request with `max_tokens > 21_333`
> (`3600 * max_tokens / 128_000 > 600s`). Every version this repo has run carries
> that guard (1.0.0 and 0.120.2 verified 2026-08-21; 1.8.0 carries the same
> formula, checked 2026-09-24, and so does 1.9.0, checked 2026-09-29) — but no test could see it, because the suite drives
> `tests/fakes.py`'s `FakeAnthropic` and never reaches the real client. That is
> why the fake now enforces the same limit itself
> (`_MAX_NONSTREAMING_MAX_TOKENS`, re-derived from the SDK's arithmetic rather
> than imported from `src`), and why `src/services/llm.py` clamps every
> truncation retry to `NONSTREAMING_MAX_TOKENS` and raises on a call site above
> it. See `tests/unit/test_llm_nonstreaming_ceiling.py`.
>
> **The SDK no longer enforces that ceiling for us, and has not since the 300 s
> client timeout landed.** `Messages.create` applies
> `_calculate_nonstreaming_timeout` only `if not stream and not
> is_given(timeout) and self._client.timeout == DEFAULT_TIMEOUT`, and
> `_client_for_key` now constructs its client with
> `anthropic.Timeout(CLIENT_READ_TIMEOUT_SECONDS, connect=5.0)`
> (`src/services/llm.py:41`, `:145`) — so that condition is permanently false
> and the SDK will happily send a request the API rejects. `_acreate`'s own
> check, which raises `NonStreamingMaxTokensError` (`:216`, raised at `:382`),
> is now the ONLY enforcement in the process. Do not remove it on the grounds
> that the SDK checks too; it does not.

Running pytest **inside the container** (`docker compose -f
docker-compose.prod.yml exec blackbird-app python -m pytest ...`) does not
currently work: the image installs with `pip install --no-cache-dir .` (`Dockerfile:43`), with no `[dev]` extra, so
pytest is not installed there (verified: `exec blackbird-app python -c "import
pytest"` → `ModuleNotFoundError`). Restoring that path would need the image (or
a test-targeted variant of it) to install `.[dev]` instead.

If that path is ever restored, `TEST_DATABASE_URL` becomes required again: the
web container has no Docker socket, so without it every test that needs a
database errors out (469 of them when that was measured on 2026-08-04 — treat
it as a floor, not a current count: the 2026-08-22 correctness branch alone
added 17 test files, many DB-backed, and the suite is now 287 `test_*.py`
files, as of 2026-09-24):

```bash
docker compose -f docker-compose.prod.yml exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/ -v
```

The service is **`blackbird-app`**, not `app` — see the two-stack warning in
`docs/operations/host-and-simulation.md` for why the `-f docker-compose.prod.yml` is not
optional.

Whichever way you run it, a database named in `TEST_DATABASE_URL` must already
exist — the suite migrates it, it does not create it. Add a fresh scratch DB
with `docker compose -f docker-compose.prod.yml exec -T postgres createdb -U
copi copi_xN`, and give concurrent suites distinct names so they do not
migrate each other's schema mid-run. Never point `TEST_DATABASE_URL` at `copi`,
the dev database.
