# Issue #27 re-verification — Deploy, CI & coverage gate (I1..I5)

Verified against `/home/a/scripps/coPI.science` @ `copi-prod` HEAD `18ba52c` (clean tree), 2026-09-02.
Method: read-only; every claim located by symbol/grep; sizes measured with `du`; `.dockerignore` applied
mechanically with a Go-`filepath.Match`-equivalent simulator; FastAPI app imported on the host and the
`/api/health` route introspected; `ruff` run on `src/` with the exact ratchet command from `ci.sh`.
No `docker compose`, no `ci.sh`, no network, no edits.

**Headline: nothing in this issue has been fixed since 2026-08-11. 24 defect sub-claims are STILL PRESENT,
one is PARTIAL (a listed file no longer exists on disk), and the I3 hole is materially worse than the issue
states: `backups/prod-sync-20260810/env.prod` is an unredacted production `.env` (146 keys; 125
`SLACK_BOT_TOKEN_*`, `POSTGRES_PASSWORD`, `DATABASE_URL`, `SLACK_CONFIG_TOKEN`/`_REFRESH_TOKEN`) that the
`.env` / `.env.*` ignore patterns do NOT match, so `COPY . .` bakes live credentials as plaintext, not just a
pg_dump.**

---

## 1. Summary table

| id | claim (one line) | verdict | key evidence | conf |
|---|---|---|---|---|
| I1-a | `ci.sh` lints `src/` under one-way ratchet `SRC_LINT_MAX=260` | CONFIRMED (accurate) | `scripts/ci.sh:55`, `:245-290` | high |
| I1-a2 | "currently 256 findings" | STALE NUMBER | measured **254** with ruff 0.15.22 (`ruff check src --output-format=concise --quiet` → 254 lines; `--statistics` → "Found 254 errors") | high |
| I1-b | `E902`-refusal so ratchet cannot fail open | CONFIRMED | `scripts/ci.sh:276-282` | high |
| I1-c | `COV_MIN=60` via `--cov-fail-under` | CONFIRMED | `scripts/ci.sh:46`, `:293-295` | high |
| I1-d | alembic gated twice: dup-id/single-head + `head→0018→head` round trip on throwaway `postgres:15` | CONFIRMED | `scripts/ci.sh:103-127`, `:70`, `:192-240` | high |
| I1-e | old "raise COV_MIN to 38" task obsolete | CONFIRMED | `git show b7edcbc:scripts/ci.sh` line 25 had `COV_MIN=35`; now 60 | high |
| I1-f | **No type check anywhere** (no mypy/pyright; ruff `E,F,I,UP,B` only) | STILL PRESENT | `pyproject.toml:68 select = ["E","F","I","UP","B"]`, `:69 ignore=["E501"]`; `git grep -i "mypy\|pyright"` → 0 hits | high |
| I1-g | **No `.github/` workflow**; `ci.sh:4-6` documents local-only by design | STILL PRESENT | `ls .github` → ENOENT; `git log --all -- .github` → empty (never existed); quote is at `ci.sh:4-5` | high |
| I1-h | gate is the local pre-push hook, bypassable with `--no-verify` | CONFIRMED | `.git/hooks/pre-push` (installed 2026-07-20) `exec …/scripts/ci.sh`; `core.hooksPath` unset; `scripts/install-hooks.sh:6` | high |
| I2-a | `/api/health` returns unconditional `{"status":"ok"}`, no `SELECT 1` | STILL PRESENT | `src/main.py:150-153`; runtime introspection: `signature: ()`, `dependant.dependencies: []` | high |
| I2-b | prod compose healthcheck probes exactly that route | STILL PRESENT | `docker-compose.prod.yml:45-50` (test on `:46`; issue said `:45`) | high |
| I2-c | nginx `depends_on: app: condition: service_healthy` | STILL PRESENT | `docker-compose.prod.yml:162-164` (unchanged line numbers) | high |
| I2-d | no `alembic upgrade` in any routine deploy path | STILL PRESENT | Dockerfile/compose/`src/main.py`: 0 alembic hits; no entrypoint; `CMD` = uvicorn; only manual README/AGENT.md steps | high |
| I2-e | `run_migration.sh` migrates with read-back, "pinned `TARGET=0024`" | CONFIRMED (minor wording) | `scripts/migrate/run_migration.sh:252-256`, `:284-293`; `TARGET="0024"` at `:56` is a **default**, overridable via `--target` (`:70`) | high |
| I2-f | live observation 2026-07-30 (200 while ORM raised `UndefinedColumnError`) | NOT REPRODUCIBLE HERE | no DB access permitted; mechanism fully consistent with I2-a | med |
| I3-a | `.dockerignore` now exists, covers `.git`, `logs`, `data`, `certbot`, `.env*`, caches | CONFIRMED | `.dockerignore:1-17`; tracked since `c46918a` (2026-08-03), absent at `b7edcbc` | high |
| I3-b | **`backups/` 668 MB of prod dumps baked by `COPY . .`** | STILL PRESENT — **WORSE** | `du -sh backups` → `668M`; simulator: BAKED; contains `copi_prod_20260810.dump` (700 MB, "PostgreSQL custom database dump v1.14") **and `env.prod` (14,439 B, plaintext prod secrets) and `env.local-testing`** | high |
| I3-c | `.venv-test/` 419 MB baked; `.venv` pattern doesn't match | STILL PRESENT | `du -sh .venv-test` → `419M`; `.dockerignore:10 .venv`; simulator: BAKED | high |
| I3-d | `tests/` 14 MB baked | STILL PRESENT | `du -sh tests` → `16M` | high |
| I3-e | `mutants/` 3 MB baked | STILL PRESENT | `3.0M`, BAKED | high |
| I3-f | `.notes/` 1.2 MB baked | STILL PRESENT | `1.2M`, BAKED | high |
| I3-g | assorted `build/`, `.hypothesis/`, `.ruff_cache`, `.coverage` ~1.7 MB | PARTIALLY | `build` 872K, `.hypothesis` 320K, `.ruff_cache` 204K all BAKED; **`.coverage` does not exist on disk**. Unlisted extras also BAKED: `.playwright-mcp` 2.07 MB, `.mutmut-cache`, `copi.egg-info`, `.superpowers`, `static/` | high |
| I3-h | runs as root (no `USER`) | STILL PRESENT | `Dockerfile` (24 lines) has no `USER` | high |
| I3-i | single-stage; `gcc`/`libpq-dev` in runtime image | STILL PRESENT | `Dockerfile:1` single `FROM`; `:6-9` apt install | high |
| I3-j | `COPY src/ src/` before `pip install .` busts dep-layer cache | STILL PRESENT | `Dockerfile:12-14` | high |
| I4-a | 17 runtime deps are bare `>=` floors (`pyproject.toml:11-29`) | STILL PRESENT (exact) | `pyproject.toml:12-28`: 17 entries, every one `>=` only | high |
| I4-b | only upper bound is dev-only `mutmut<3` | CONFIRMED | `pyproject.toml:44` | high |
| I4-c | `anthropic>=0.26.0` floats across majors | STILL PRESENT | `pyproject.toml:21`; host venv resolves to `anthropic 0.117.0` | high |
| I4-d | `plotly>=5.20.0` runtime dep; sole importer `scripts/build_cabo_sankey.py` | STILL PRESENT | `pyproject.toml:28`; `git grep plotly` → only `scripts/build_cabo_sankey.py:29,128`; 0 hits in `src/`, `templates/` | high |
| I4-e | no lockfile of any kind | STILL PRESENT | none of `requirements*.txt`/`uv.lock`/`poetry.lock`/`Pipfile.lock`/`pdm.lock`/`constraints*`; `Dockerfile:14 pip install --no-cache-dir .` (no hashes/constraints) | high |
| I4-f | ships in all four images | CONFIRMED | app/worker/agent/grantbot all `build: context: .` (`docker-compose.prod.yml:26,60,85,113`) | high |
| I5-a | "Next.js … port 3000" comments at `:14/:37/:69/:127` | STILL PRESENT (+1) | exact; plus a 5th at `nginx/nginx.conf:166`; upstream is `app:8000` (`:38-40`) | high |
| I5-b | `proxy_read_timeout 120s` in four places `:124/:145/:237/:302` | STILL PRESENT (exact) | grep → exactly those 4 lines | high |
| I5-c | no CSP anywhere in `nginx/`; three vhosts carry the same four headers | STILL PRESENT | 0 hits; headers at `:98-101`, `:217-220`, `:283-286`; app CSP only `src/routers/public.py:50,88` | high |
| I5-d | dead `proxy_cache_valid` w/o `proxy_cache_path` inside `location /_next/static/` matching no route | STILL PRESENT | `nginx.conf:167-171`; `proxy_cache_path` 0 hits; no `_next` route in `src/`/`templates/`; static is at `/static` (`src/main.py:136`) | high |
| I5-e | no `mem_limit`/`cpus` on any of 7 prod services | STILL PRESENT | grep `mem_limit\|cpus\|deploy:\|resources` across all 3 compose files → 0; 7 services parsed | high |
| I5-f | `./prompts` bind-mounted on 3 services (`app:39`, `agent:92`, `grantbot:121`) | STILL PRESENT (lines drifted) | now `docker-compose.prod.yml:41`, `:97`, `:126`. **Extra:** `worker` has no prompts mount yet reads `prompts/profile-synthesis.md` (`src/worker/main.py:18` → `profile_pipeline.py:26,310` → `services/llm.py:38`) — worker uses the baked copy, the others the host copy | high |
| I5-g | SEC-15 rate-limit zones already added | CONFIRMED (but incomplete) | `nginx.conf:29-35,107,112,130` — **only on the `${DOMAIN}` vhost**; devel/blackbird vhosts have no `limit_req`/`limit_conn`; blackbird also lacks `ssl_ciphers`/OCSP/`resolver` | high |
| I5-h | cross-issue: 120s timeout turns #24's blocking call into a 504 | N/A (not in scope) | 120s value confirmed; #24 mechanism not re-verified here | — |
| P-1 | Priority: `.gitignore:86-88` describes backups as "message bodies and tokens" | CONFIRMED (line drift) | now `.gitignore:89-91` | high |

**Counts:** 24 still present · 0 fixed · 1 partial (I3-g) · 0 changed · 1 not reproducible here (I2-f) · 1 N/A (I5-h) · 13 descriptive claims confirmed accurate (I1-a..e, I1-h, I2-e, I3-a, I4-b, I4-f, I5-g, P-1, I1-a2 stale-number).

---

## 2. Per-item detail

### PR I1 — CI gate

**What `ci.sh` does now (verified by reading the whole 301-line script):**

```
scripts/ci.sh:4-5   # installed the hook (scripts/install-hooks.sh). There is NO server-side CI and no
                    # GitHub-side hooks by design — this script is the whole gate, and it runs on push.
scripts/ci.sh:46    COV_MIN="${COV_MIN:-60}"
scripts/ci.sh:55    SRC_LINT_MAX="${SRC_LINT_MAX:-260}"
scripts/ci.sh:70    MIGRATION_FLOOR="${MIGRATION_FLOOR:-0018}"
scripts/ci.sh:109   dupes="$(grep -h '^revision' alembic/versions/*.py | sort | uniq -d || true)"
scripts/ci.sh:119   heads_out="$("$VENV_PY" -m alembic heads 2>/dev/null || true)"
scripts/ci.sh:121   if [ "$heads_n" -ne 1 ]; then
scripts/ci.sh:210   docker run -d --name "$MIGCHECK_CONTAINER" ... -p "127.0.0.1:${MIGCHECK_PORT}:5432" postgres:15
scripts/ci.sh:229   DATABASE_URL="$migration_dsn" "$VENV_PY" -m alembic upgrade head
scripts/ci.sh:230   DATABASE_URL="$migration_dsn" "$VENV_PY" -m alembic downgrade "$MIGRATION_FLOOR"
scripts/ci.sh:231   DATABASE_URL="$migration_dsn" "$VENV_PY" -m alembic upgrade head
scripts/ci.sh:243   "$VENV_PY" -m ruff check "${LINT_TARGETS[@]}"        # tests/*, scripts/migrate, scripts/backup → zero findings
scripts/ci.sh:261   src_lint_out="$("$VENV_PY" -m ruff check src --output-format=concise --quiet 2>&1)"
scripts/ci.sh:264   if [ "$src_lint_rc" -gt 1 ]; then ... exit 1        # ruff itself failed → gate fails
scripts/ci.sh:276   if printf '%s' "$src_lint_out" | grep -q 'E902'; then ... exit 1
scripts/ci.sh:283   src_findings="$(printf '%s' "$src_lint_out" | grep -c . || true)"
scripts/ci.sh:284   if [ "$src_findings" -gt "$SRC_LINT_MAX" ]; then ... exit 1
scripts/ci.sh:293   "$VENV_PY" -m pytest tests/ --cov=src --cov-report=term-missing --cov-fail-under="${COV_MIN}"
```

Note the ratchet ceiling `SRC_LINT_MAX` and the coverage floor `COV_MIN` are both env-overridable
(`COV_MIN=0 ./scripts/ci.sh` would pass a 0% run) — that is the design, and the issue does not claim otherwise,
but it means "one-way ratchet" is a convention enforced by comments (`:52-54`), not by code.

**Ruff ratchet — ran the exact ci.sh command on the host:**

```
$ .venv-test/bin/python -m ruff --version            → ruff 0.15.22
$ .venv-test/bin/python -m ruff check src --output-format=concise --quiet | grep -c .   → 254
$ .venv-test/bin/python -m ruff check src/ --statistics
150 B008  function-call-in-default-argument
 27 F821  undefined-name
 26 UP017 datetime-timezone-utc
 14 I001  unsorted-imports
  9 B904  raise-without-from-inside-except
  9 E402  module-import-not-at-top-of-file
  7 F401  unused-import
  5 F841  unused-variable
  4 B007  unused-loop-control-variable
  2 UP035 deprecated-import
  1 E741  ambiguous-variable-name
Found 254 errors.
```
254 vs ceiling 260 (6 of slack). The issue's "currently 256" is stale by 2; `ruff>=0.4.0` is a bare floor
(`pyproject.toml:35`), so the count depends on the installed ruff version — the ratchet is not version-pinned.
`ruff check tests/ --statistics` → 0 findings, exit 0 (the "spotless" tier holds).

**I1-f No type check — STILL PRESENT.** `pyproject.toml:63-69`:
```toml
[tool.ruff]
line-length = 100
target-version = "py311"
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]
```
`git grep -c -i -E "mypy|pyright" -- . ':!.notes'` → exit 1 (zero matches in any tracked file). No `[tool.mypy]`,
no `py.typed`, no pyright config. The 27 `F821 undefined-name` findings ruff already reports in `src/` are a hint
of what a type checker would surface.

**I1-g No `.github/` — STILL PRESENT.** `ls -la .github` → "No such file or directory"; `git ls-files .github` →
empty; `git log --all --oneline -- .github` → empty (never existed in any branch). The "by design" rationale the
issue quotes is at `ci.sh:4-5` (not 4-6). `scripts/install-hooks.sh:3-4` repeats it.

**I1-h pre-push hook** — installed and current: `.git/hooks/pre-push` (224 B, 2026-07-20, `+x`):
```
exec "$(git rev-parse --show-toplevel)/scripts/ci.sh"
```
It is the only non-sample hook; `git config core.hooksPath` is unset. Bypass documented at
`install-hooks.sh:6` and hook line 3.

**Tests:** `tests/unit/test_migration_checks.py` pins the alembic static properties; nothing tests `ci.sh` itself
(it is bash). No test would notice removal of the type-check that does not exist.

### PR I2 — `/api/health` + migrate step

**I2-a — STILL PRESENT.** `src/main.py:150-153`:
```python
    @application.get("/api/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}
```
Mechanical check — imported the app on the host
(`SECRET_KEY=x ENVIRONMENT=development DATABASE_URL=postgresql+asyncpg://u:p@localhost/x`) and introspected the
route object:
```
path: /api/health methods: {'GET'}
signature: ()
dependant.dependencies: []
```
Zero dependencies, zero parameters — it cannot touch the DB. `grep -n "lifespan\|on_event\|startup\|create_all\|alembic\|SELECT\|get_db\|engine" src/main.py`
→ only an unrelated comment at `:111`; there is no startup DB probe either. `git log -S"SELECT 1" -- src/main.py` → nothing.

**I2-b/c — STILL PRESENT.** `docker-compose.prod.yml:45-50`:
```yaml
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen(\\\"http://127.0.0.1:8000/api/health\\\")\""]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
```
`docker-compose.prod.yml:162-164`:
```yaml
    depends_on:
      app:
        condition: service_healthy
```
So the chain the issue describes (uvicorn up ⇒ healthy ⇒ nginx starts) is exactly as coded. `worker`, `agent`,
`grantbot` have no healthcheck at all (parsed compose: `healthcheck=None`).

**I2-d — STILL PRESENT.** `grep -n alembic Dockerfile docker-compose*.yml` → 0 hits. `Dockerfile:24
CMD ["uvicorn", "src.main:app", ...]`; no ENTRYPOINT, no entrypoint script anywhere
(`find . -iname "*entrypoint*" -o -iname "deploy*.sh"` → none outside venv). `git grep "create_all\|command.upgrade\|alembic upgrade" -- src/` → 0.
Documented deploy commands are all bare `docker compose up -d --build`: `README.md:39,83`, `CLAUDE.md:96`,
`docs/production-migration.md:442`, `docs/issue-29-remediation.md:28`. Mitigation the issue did not mention:
`README.md:45-47` and `AGENT.md:145` document a **manual** `alembic heads / upgrade head / current` step — an
instruction, not a gate. The only automated migrate is the CI throwaway round-trip (`ci.sh:229-231`), which
never touches a deployed DB.

**I2-e — CONFIRMED, minor wording.** `scripts/migrate/run_migration.sh`:
```
:56   TARGET="0024"
:70       --target) TARGET="${2:?--target needs a revision}"; shift 2 ;;
:252  echo "--- Step 5: alembic upgrade $TARGET (lock_timeout ${LOCK_TIMEOUT_MS}ms) ---"
:256    python -m alembic upgrade "$TARGET"
:284          r = await c.execute(sa.text('select version_num from alembic_version'))
:288  if [ "$STAMP" != "$TARGET" ]; then ... BLOCKED
```
"Pinned" is slightly off — it is a default overridable by `--target` — but the substance (one-time gated runbook
with preflight/backup/postflight, not a deploy step) holds. Last touched `c70b48b` ("advance … 0023 to 0024").

**Tests:** `tests/integration/test_health_route.py:6-9` asserts `200` and `== {"status": "ok"}` — it pins the
present response shape and has no DB-down case. `tests/unit/test_reachability.py:116-119` allow-lists the route as
"Liveness probe … docker-compose/nginx … hit it directly." A `SELECT 1` fix would keep both green.

### PR I3 — Dockerfile hardening / `.dockerignore` hole

**`.dockerignore` (tracked, `c46918a` 2026-08-03; not present at `b7edcbc`):**
```
.git  .gitignore  certbot  logs  data  *.log  __pycache__  **/__pycache__  *.pyc  .venv  venv
.pytest_cache  .provision_state.json  .env  .env.*
```

**Simulation method.** Docker (moby/patternmatcher) matches each pattern with Go `filepath.Match` semantics against
the context-relative path and each of its parent prefixes; `*` does not cross `/`; a matched directory excludes
its subtree. I implemented that in Python (`**/` prefix stripped; `*`→`[^/]*`; tested each path and every parent
prefix) and ran it over every top-level entry plus the three files inside `backups/`. `du -sb` for byte sizes.

```
   700.02 MB  BAKED                        backups
   393.07 MB  BAKED                        .venv-test
    23.34 MB  EXCLUDED by .git             .git
    15.92 MB  BAKED                        tests
     3.72 MB  BAKED                        src
     2.63 MB  BAKED                        mutants
     2.07 MB  BAKED                        .playwright-mcp      ← not in the issue's table
     1.16 MB  BAKED                        .notes
     0.74 MB  BAKED                        build
     0.28 MB  EXCLUDED by .pytest_cache    .pytest_cache
     0.08 MB  BAKED                        .ruff_cache
     0.04 MB  BAKED                        .hypothesis
     0.04 MB  BAKED                        .mutmut-cache        ← not in the issue's table
     0.01 MB  BAKED                        copi.egg-info        ← not in the issue's table
     0.00 MB  EXCLUDED by .env             .env
     0.00 MB  EXCLUDED by .env.*           .env.example
     0.00 MB  EXCLUDED by data             data
TOTAL entering the build context: 1122.5 MB   (of which ~1093 MB is backups + .venv-test)

backups/prod-sync-20260810/env.prod                 -> BAKED
backups/prod-sync-20260810/env.local-testing        -> BAKED
backups/prod-sync-20260810/copi_prod_20260810.dump  -> BAKED
.coverage                                            exists=False
```
`du -sh` (disk usage, what the issue quoted): `668M backups`, `419M .venv-test`, `16M tests`, `3.0M mutants`,
`1.2M .notes`, `872K build`, `320K .hypothesis`, `204K .ruff_cache`. The issue's 668 MB / 419 MB figures are exact.

**I3-b is worse than described.** `ls -la backups/prod-sync-20260810/`:
```
-rw------- 699997302  copi_prod_20260810.dump    file: "PostgreSQL custom database dump - v1.14-0"
-rw-------      4196  env.local-testing          file: UTF-8 text
-rw-------     14439  env.prod                   file: UTF-8 text
```
`head -c 200 env.prod` shows `POSTGRES_USER=copi`, `POSTGRES_PASSWORD=<48-hex, REDACTED>`,
`DATABASE_URL=postgresql+asyncpg://copi:<REDACTED>@…`. Key census (names only, values not read):
146 distinct keys; **125 `SLACK_BOT_TOKEN_*`**; 133 keys matching `TOKEN|SECRET|KEY|PASSWORD`;
`SLACK_CONFIG_TOKEN`, `SLACK_CONFIG_REFRESH_TOKEN` present. `env.local-testing`'s own header
(`backups/…/env.local-testing:1-5`) says: "Faithful archive: env.prod (same directory, keep 0600, never commit).
Stripped here: 125 SLACK_BOT_TOKEN_* (production workspace), the SLACK_CONFIG_TOKEN/SLACK_CONFIG_REFRESH_TOKEN
pair … and POSTHOG_API_KEY".

Why the existing `.env` / `.env.*` patterns do not help: they are root-anchored and match only names beginning
with `.env`; `backups/prod-sync-20260810/env.prod` has no leading dot and is two directories down. The issue
framed the leak as "message bodies and tokens" inside a pg_dump (true — `.gitignore:89-91`); the plaintext
production `.env` sitting beside it is the sharper problem. `COPY . .` (`Dockerfile:17`) would place it at
`/app/backups/prod-sync-20260810/env.prod` in every app/worker/agent/grantbot image, readable by the root process.
(The `0600` mode is preserved by COPY but irrelevant when the container runs as root — see I3-h.)

I could not confirm whether an image has actually been built since 2026-08-10 (that needs `docker`, which is
out of bounds), so "has leaked" vs "will leak on next build" is unverified; the memory note says prod images
were stale as of 08-14, but this repo is the local copy — `backups/` exists here, and the same `COPY . .` runs
wherever the tree is built.

**I3-h/i/j — STILL PRESENT.** Full `Dockerfile` (unchanged since `7675e98`, 2026-07-09):
```
 1  FROM python:3.11-slim
 3  WORKDIR /app
 6  RUN apt-get update && apt-get install -y --no-install-recommends \
 7      gcc \
 8      libpq-dev \
 9      && rm -rf /var/lib/apt/lists/*
12  COPY pyproject.toml .
13  COPY src/ src/
14  RUN pip install --no-cache-dir .
17  COPY . .
20  RUN mkdir -p profiles/public profiles/private prompts logs static
22  EXPOSE 8000
24  CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
No `USER`; single stage; `gcc`+`libpq-dev` remain (`asyncpg` ships wheels — the build deps are likely unneeded
even at install time). `COPY src/ src/` precedes `pip install .` because the non-editable install needs the
package present (`pyproject.toml:59-61 packages.find include=["src*"]`), so any `src/` edit invalidates the
pip layer; the fix needs a deps-first install (lockfile → `pip install -r`), i.e. it is coupled to I4.

**Tests:** none reference `.dockerignore`/`Dockerfile` (`git grep -l dockerignore -- tests/` → none).

### PR I4 — Lockfile + pinning

`pyproject.toml:11-29` (exact 17 runtime entries):
```
fastapi>=0.111.0  uvicorn[standard]>=0.29.0  jinja2>=3.1.4  python-multipart>=0.0.9  authlib>=1.3.0
httpx>=0.27.0  sqlalchemy[asyncio]>=2.0.30  asyncpg>=0.29.0  alembic>=1.13.0  anthropic>=0.26.0
slack-sdk>=3.27.0  pydantic-settings>=2.2.0  itsdangerous>=2.2.0  boto3>=1.34.0  typer>=0.12.0
rich>=13.7.0  plotly>=5.20.0
```
17/17 bare `>=`; zero `<`, `==`, `~=`. Dev extras (`:32-54`) are also floors except `mutmut>=2.4,<3` (`:44`).
`anthropic>=0.26.0` resolves today to `0.117.0` in `.venv-test` (verified via import) — 90+ minor releases past
the floor with no cap. Lockfile search (explicit names + `find` + `git ls-files | grep -i 'lock\|requirements'`)
→ none anywhere in the tree. `Dockerfile:14 pip install --no-cache-dir .` resolves fresh at every build with no
`--require-hashes`/`-c constraints`.

plotly: `git grep -n plotly -- '*.py' '*.toml'` → `pyproject.toml:28`, `scripts/build_cabo_sankey.py:29`
(`import plotly.graph_objects as go`), `:128`. `git grep -c plotly -- src/ templates/` → 0. It is dead weight in
all four `build: context: .` images (`docker-compose.prod.yml:26,60,85,113`).

Nothing in the issue is wrong here; line refs `11-29` are still exact.

### PR I5 — nginx + resource limits

There is exactly one nginx file: `nginx/nginx.conf` (307 lines), mounted as the envsubst template
(`docker-compose.prod.yml:157`). Six `server {}` blocks = 3 vhosts × (HTTP redirect + HTTPS): `${DOMAIN}`
(`:52`, `:71`), `devel.copi.science` (`:177`, `:195`), `blackbird.copi.science` (`:254`, `:268`).

**I5-a — STILL PRESENT (+1).**
```
nginx.conf:14   #   - Reverse proxy to Next.js app on port 3000
nginx.conf:37   # Upstream definition for the Next.js app
nginx.conf:38   upstream app { server app:8000; }
nginx.conf:69   # HTTPS server — reverse proxy to Next.js app
nginx.conf:127      # Reverse proxy to Next.js app
nginx.conf:166      # Cache Next.js static assets (hashed filenames, safe to cache forever)   ← unlisted 5th
```
**I5-b — exact.** `proxy_read_timeout 120s` at `:124` (graph routes), `:145` (`location /`), `:237` (devel), `:302` (blackbird).

**I5-c — STILL PRESENT.** `grep -rn Content-Security-Policy nginx/` → 0. Each HTTPS vhost has the same four
`add_header` lines (HSTS, X-Frame-Options DENY, X-Content-Type-Options, Referrer-Policy) at `:98-101`,
`:217-220`, `:283-286`. App-side CSP exists only in `src/routers/public.py:50` (`_graph_csp`) and `:88`
(`response.headers["Content-Security-Policy"]`), for the standalone graph pages — as the issue says.

**I5-d — STILL PRESENT.** `nginx.conf:167-171`:
```
    location /_next/static/ {
        proxy_pass http://app;
        proxy_cache_valid 200 365d;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }
```
`proxy_cache_path` → 0 hits (so `proxy_cache_valid` is inert). `git grep _next -- src/ templates/ static/` → only
unrelated identifiers (`_next_poll_client`, `is_safe_next_url`, `call_next`). The app's static mount is
`/static` (`src/main.py:136`), which gets no nginx cache rule.

**I5-e — STILL PRESENT.** `grep -n "mem_limit\|cpus\|deploy:\|resources\|limits\|memory\|pids_limit\|ulimits"`
over `docker-compose.prod.yml`, `docker-compose.override.yml`, `docker-compose.yml` → 0 hits. Seven prod services
parsed: postgres, app, worker, agent, grantbot, nginx, certbot.

**I5-f — STILL PRESENT, line refs drifted, plus an unreported inconsistency.** `./prompts:/app/prompts` at
`docker-compose.prod.yml:41` (app), `:97` (agent), `:126` (grantbot); issue said 39/92/121. The image also bakes
`prompts/` (not in `.dockerignore`; `Dockerfile:17,20`). `worker` (`:59-82`) mounts only `./profiles`, yet its
job path reads a prompt from disk: `src/worker/main.py:18 from src.services.profile_pipeline import run_profile_pipeline`
→ `src/services/profile_pipeline.py:26,310 synthesize_profile` → `src/services/llm.py:38
prompt_path = "prompts/profile-synthesis.md"` (with a hard-coded fallback at `:42`). So after a host-side prompt
edit without a rebuild, app/agent/grantbot run the new text and worker runs the old — the "silent shadowing" the
issue flags has a concrete split-brain consequence it did not spell out.

**I5-g — SEC-15 zones present but only on one vhost.** `limit_req_zone`/`limit_conn_zone` at `:29-31`,
`limit_conn conn_perip 30` at `:107`, `limit_req` at `:112` and `:130` — all inside the `${DOMAIN}` HTTPS server.
The `devel` and `blackbird` HTTPS servers (`:195-243`, `:268-307`) have **no** `limit_req`/`limit_conn`. The
blackbird server also omits `ssl_ciphers`, `ssl_stapling`, and `resolver` that the other two carry
(`ssl_ciphers` at `:83,205` only; `ssl_stapling` at `:92-93,212-213` only). The issue's "propagated the defects"
is directionally right (timeouts, headers, comments), but the copies are not faithful — they drop hardening too.
This strengthens the "reconcile the template" fix.

**I5-h** (#24 cross-reference) — not re-verified; out of scope for this file set.

---

## 3. Counts

**24 still present · 0 fixed · 1 partial · 0 changed · 1 not reproducible here · 1 N/A · 13 descriptive
claims confirmed accurate (2 with drifted line numbers, 1 with a stale count).**

## 4. Where the issue text is stale or imprecise (defects unaffected)

- `ci.sh:4-6` → the quoted sentence is at `ci.sh:4-5`.
- "currently 256 findings" → 254 (ruff 0.15.22 on host; ruff is itself an unpinned floor so this drifts).
- `main.py:150-152` → `150-153`. `docker-compose.prod.yml:45` → healthcheck block `45-50`, `test:` on `46`.
- `.gitignore:86-88` → `89-91`.
- prompts mounts `app:39 / agent:92 / grantbot:121` → `41 / 97 / 126`.
- I3 table lists `.coverage` — the file does not exist on disk; `.playwright-mcp` (2.07 MB), `.mutmut-cache`,
  `copi.egg-info`, `.superpowers`, `static/` are baked and unlisted. `tests/` is 16 MB not 14.
- "pinned `TARGET=0024`" → default, overridable with `--target` (`run_migration.sh:56,70`).
- Four "Next.js" comments listed; there are five (`:166`).
- I3-b understates the leak: `backups/` holds a plaintext production `.env` (`env.prod`), not only the dump.
- I5-g implies SEC-15 is done; it covers one of three vhosts.

## 5. What I could not verify and why

- **Whether any image has actually been built with `backups/` present** (i.e. whether the secret leak is
  realised or latent). Requires `docker image history`/`docker build`, which the constraints forbid. `backups/`
  is dated 2026-08-10; `Dockerfile` has not changed since 2026-07-09.
- **The 2026-07-30 live observation** (200 from `/api/health` while ORM reads failed). No DB or container
  access; the route introspection (zero dependencies) makes it mechanically inevitable, so I rate the claim
  credible but unreproduced.
- **I5-h** (#24 → 504 interaction). Only the 120 s value was verified; the blocking-call side lives in another issue.
- **Whether prod nginx currently serves this exact `nginx.conf`** — it is bind-mounted read-only
  (`docker-compose.prod.yml:157`) so the file *is* the config, but I did not touch the running container.
- I did not open the pg_dump beyond `file` + `head -c 200` (per instructions), so "message bodies and tokens" in
  the dump rests on `.gitignore:89-90`'s description and the dump's 700 MB size, not on inspection.
