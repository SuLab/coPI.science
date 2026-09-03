# #27 Deploy, CI & coverage gate: no GitHub Actions, fake healthcheck, unpinned deps, stale nginx (5 PRs)
state=OPEN created=2026-07-30T15:53:12Z updated=2026-08-11T23:54:41Z labels=['area:infra', 'verified-2026-07-30']

## BODY

Deploy/supply-chain items.

Originally verified at `origin/main` @ `b7edcbc` (2026-07-30). **Re-verified 2026-08-11 against the open PR-stack tip (`issue-29-authorship-grounding` @ `b1d54da` = main + #30/#31/#32). The stack substantially rebuilt the local CI gate (I1) and added a `.dockerignore` (I3) — but re-verification found the I3 residual is now the most serious item in this issue.**

## Priority (triage 2026-08-11)

1. **I3's remaining `.dockerignore` hole — do before the next image build** (one commit): `COPY . .` currently bakes **`backups/` (668 MB of full production DB dumps — message bodies and tokens, per `.gitignore`'s own comment)** plus `.venv-test/` (419 MB) into every image layer.
2. **I2 is Tier 1** — the fake healthcheck marked a broken app healthy in a live incident (2026-07-30) and nothing in a routine deploy migrates.
3. I4 and I5 are Tier 3 batches. I1's residual is a type check plus a decision on the (documented) local-only CI stance.

**Suggested order: I3-hole → I2 → I4 → I5 → I1-residual.**

### PR I1 — CI gate *(largely rebuilt by the stack; two residuals)*
The stack's `scripts/ci.sh` now: lints `src/` under a one-way ratchet (`SRC_LINT_MAX=260`, currently 256 findings, with `E902`-refusal so the ratchet can't fail open); enforces `COV_MIN=60` via `--cov-fail-under` (re-baselined from 35 after fixing the branch-coverage tracer — the old 35 was measured against a broken tracer; true coverage was ~61.6 %); and gates alembic twice — duplicate-revision/single-head, plus a full `head → 0018 → head` round trip against a self-provisioned throwaway `postgres:15`. The old "raise `COV_MIN` to 38" task is obsolete.

Residuals:
- **No type check anywhere** (no mypy/pyright; ruff selects `E,F,I,UP,B` only).
- **No `.github/` workflow** — but note `ci.sh:4-6` now documents this as deliberate ("There is NO server-side CI and no GitHub-side hooks by design"; the whole gate is the local pre-push hook, bypassable with `--no-verify`). So GitHub Actions is a *decision to revisit*, not an oversight to fix silently. If the local-only stance stands, the actionable remainder of I1 is just the type check.

> **Definition of done for every issue in this backlog:** each PR ships a test that covers its defect line and fails against the pre-fix code. Issues #20, #21 and #23 touch the largest low-coverage files and therefore carry most of the ratchet.

### PR I2 — Real `/api/health` DB check + a migrate step *(small — live failure observed)*
Still present, all three. `main.py:150-152` `/api/health` returns an unconditional `{"status":"ok"}` with no `SELECT 1`; the prod compose healthcheck (`docker-compose.prod.yml:45`) probes exactly that route and nginx `depends_on: service_healthy` (`:162-164`) — so nginx starts, and Docker keeps `app` marked healthy, with Postgres unreachable or the schema behind. No `alembic upgrade` exists in any routine deploy path: `scripts/migrate/run_migration.sh` does migrate (`:252-256`, with read-back verification), but it is a one-time gated runbook with a pinned `TARGET=0024`, not a deploy step — nothing makes an ordinary `docker compose up -d --build` migrate.

**Observed live on 2026-07-30:** with the dev DB at `0018` and code requiring `0021`, `GET /api/health` → `HTTP 200 {"status":"ok"}` while every ORM read of `agent_messages` raised `UndefinedColumnError`.

*Fix:* a migrate step gated before app traffic + a `SELECT 1` in the health route.

### PR I3 — Dockerfile hardening *(medium — the residual is a secret leak)*
`.dockerignore` now exists (covers `.git`, `logs`, `data`, `certbot`, `.env*`, caches). **Still baked by `COPY . .`:**

| path | size | note |
|---|---|---|
| `backups/` | **668 MB** | full production DB dumps — `.gitignore:86-88` itself describes them as "containing message bodies and tokens". **A secret leak into image layers; this supersedes `.notes` as the reason to fix this item.** |
| `.venv-test/` | 419 MB | the `.venv` pattern does not match `.venv-test` |
| `tests/` | 14 MB | |
| `mutants/` | 3 MB | |
| `.notes/` | 1.2 MB | the audit docs, as originally flagged |
| assorted (`build/`, `.hypothesis/`, `.ruff_cache`, `.coverage`, …) | ~1.7 MB | |

Also still present: runs as root (no `USER`), single-stage (`gcc`/`libpq-dev` ship in the runtime image), `COPY src/ src/` before `pip install .` busts the dependency layer cache on every source change.

*Fix:* extend `.dockerignore` (at minimum `backups`, `.venv-test`, `tests`, `.notes`, `mutants`) **before the next image build**; then non-root `USER`, multi-stage, and the layer-order split.

### PR I4 — Lockfile + dependency pinning *(medium)*
Still present, all four: 17 runtime deps are bare `>=` floors with no upper caps (`pyproject.toml:11-29`; the only upper bound in the file is the dev-only `mutmut<3`); `anthropic>=0.26.0` floats across majors; `plotly>=5.20.0` is a runtime dep whose only importer is `scripts/build_cabo_sankey.py` — it ships in all four images for nothing; no lockfile of any kind exists. *Fix:* a hash-pinned lockfile installed in the Dockerfile; raise floors past known CVEs; cap pre-1.0/major SDKs; move `plotly` to dev/scripts.

### PR I5 — Reconcile nginx.conf + add resource limits *(small-medium)*
Still present — and the stack *propagated* the defects into the two new vhosts (`devel.copi.science`, `blackbird.copi.science`) rather than fixing them: "Next.js app on port 3000" comments persist (`:14/:37/:69/:127`; the upstream is `app:8000`); `proxy_read_timeout 120s` now appears in **four** places (`:124/:145/:237/:302`); no CSP header anywhere in `nginx/` (all three vhosts carry the same four headers; the only CSP is app-level and scoped to the standalone graph pages); dead `proxy_cache_valid` with no `proxy_cache_path`, inside a `location /_next/static/` that matches no route in this app. No `mem_limit`/`cpus` on any of the 7 prod services; `./prompts` is bind-mounted over the baked copy on **three** services (`app:39`, `agent:92`, `grantbot:121`), silently shadowing the image's prompts. *(SEC-15 rate-limit zones were already added.)* *Fix:* reconcile the nginx template across all three vhosts; add container resource limits.
- **Cross-issue:** the 120 s read timeout is what turns PR C2's blocking provisioning call (issue #24) into a 504. Fixing either alone leaves the other symptom.


