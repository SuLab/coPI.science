# Issue #27 — red-team pass (second reviewer)

Tree: `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c, 2026-09-02. Read-only on the repo; `docker images` /
`docker history` / `docker image inspect` / `docker save | tar` used READ-ONLY on the local daemon (no container
created or started; the coordinator allowed the image-inspection step). `env.prod` was never opened.

## 1. Summary table

| id | first-agent verdict | red-team | reason | evidence |
|---|---|---|---|---|
| I1-a | CONFIRMED | UPHELD | `SRC_LINT_MAX="${SRC_LINT_MAX:-260}"` | `scripts/ci.sh:55` |
| I1-a2 | STALE (254) | UPHELD | re-ran exact ratchet cmd: **254** (ruff 0.15.22) | `.venv-test/bin/python -m ruff check src --output-format=concise --quiet \| grep -c .` → 254 |
| I1-b | CONFIRMED | UPHELD | E902 refusal present | `ci.sh:276-282` |
| I1-c | CONFIRMED | UPHELD | `COV_MIN="${COV_MIN:-60}"`, `--cov-fail-under` | `ci.sh:46`, `:293-295` |
| I1-d | CONFIRMED | UPHELD | dup-id + single-head at `:109-121`; `docker run … postgres:15` `:210`; `upgrade head / downgrade $MIGRATION_FLOOR / upgrade head` `:229-231` | grep output |
| I1-e | CONFIRMED | UPHELD | `git show b7edcbc:scripts/ci.sh` line 25 `COV_MIN="${COV_MIN:-35}"` | command output |
| I1-f | STILL PRESENT | UPHELD | `select = ["E","F","I","UP","B"]`; `git grep -i mypy\|pyright` (excl. .notes/docs) → exit 1 | `pyproject.toml:68` |
| I1-g | STILL PRESENT | UPHELD | `.github` ENOENT; quote at `ci.sh:4-5` | `ls .github` |
| I1-h | CONFIRMED | UPHELD | hook = `exec …/scripts/ci.sh`; `core.hooksPath` unset | `.git/hooks/pre-push:5` |
| I2-a | STILL PRESENT | UPHELD | route body is `return {"status": "ok"}`; no other health route in `src/` (`git grep -i health -- src/` → only `main.py:150-152` + a grantbot prose line) | `src/main.py:150-153` |
| I2-b | STILL PRESENT | UPHELD | `healthcheck:` block `docker-compose.prod.yml:45-50` | grep |
| I2-c | STILL PRESENT | UPHELD | `condition: service_healthy` at `:164` under nginx | grep |
| I2-d | STILL PRESENT | UPHELD | no Makefile, no `deploy*`/`restart*`/entrypoint file anywhere outside `.venv-test`; `alembic upgrade` only in `ci.sh:229-231` (throwaway) and `scripts/migrate/run_migration.sh:256` (runbook) | `find`/`git grep` |
| I2-e | CONFIRMED (wording) | UPHELD | `TARGET="0024"` `:56` is a default; `--target` `:70`; read-back `:284-293` | sed |
| I2-f | NOT REPRODUCIBLE | UPHELD | no DB here; mechanism inevitable given I2-a | — |
| I3-a | CONFIRMED | UPHELD | single commit `c46918a` 2026-08-03; `.env`/`.env.*` present from that first commit | `git log -- .dockerignore` |
| I3-b | STILL PRESENT — WORSE | **UPHELD + STRENGTHENED** | semantics confirmed mechanically with Go `filepath.Match` (below); AND the leak is **realised** in a local image, not latent (see NEW-1) | Go run; `docker save` layer listing |
| I3-c | STILL PRESENT | UPHELD | `.venv` pattern ≠ `.venv-test` (Go: `.venv` vs `.venv-test` no match, same rule); `du` 420M | `.dockerignore:10` |
| I3-d | STILL PRESENT | UPHELD | `tests` 17M du | — |
| I3-e | STILL PRESENT | UPHELD | `mutants` 3.0M | — |
| I3-f | STILL PRESENT | UPHELD | `.notes` 1.2M | — |
| I3-g | PARTIALLY (`.coverage` missing) | **QUALIFIED → STILL PRESENT in full** | `.coverage` DOES exist: `-rw-r--r-- 589824 Sep 2 08:33 .coverage` (created minutes before/while the first agent ran). Extras list (`.playwright-mcp`, `.mutmut-cache`, `copi.egg-info`) confirmed by `du` | `ls -la .coverage` |
| I3-h | STILL PRESENT | UPHELD | no `USER` in 24-line Dockerfile | `Dockerfile` |
| I3-i | STILL PRESENT | UPHELD | single `FROM`; gcc/libpq-dev `:6-9` | `Dockerfile:1,6-9` |
| I3-j | STILL PRESENT | UPHELD | `COPY src/ src/` `:13` before `pip install .` `:14` | `Dockerfile:12-14` |
| I4-a | STILL PRESENT | UPHELD | 17 entries `:12-28`, all `>=` | `pyproject.toml` |
| I4-b | CONFIRMED | UPHELD | `mutmut>=2.4,<3` `:44` | — |
| I4-c | STILL PRESENT | UPHELD | `anthropic>=0.26.0` `:21` | — |
| I4-d | STILL PRESENT | UPHELD | `git grep plotly` → `pyproject.toml:28`, `scripts/build_cabo_sankey.py:29,128` only | — |
| I4-e | STILL PRESENT | UPHELD | `git ls-files \| grep -i 'lock\|requirements\|constraint'` → none; no `[tool.uv]`/`[tool.pip]`/constraints in pyproject; `find -maxdepth 2` → none | — |
| I4-f | CONFIRMED | UPHELD | `build: context: .` at `:26,60,85,113` | compose |
| I5-a | STILL PRESENT (+1) | UPHELD | `Next.js` at `:14,37,69,127,166` = 5 | grep |
| I5-b | STILL PRESENT | UPHELD | `proxy_read_timeout 120s` at `:124,145,237,302`; **no `location /admin`** → admin traffic uses `location /` (`:128`) 120 s; nothing overrides per-location | grep |
| I5-c | STILL PRESENT | UPHELD | 0 CSP in `nginx/`; 4 headers ×3 vhosts `:98-101,217-220,283-286` | grep |
| I5-d | STILL PRESENT | UPHELD | `proxy_cache_valid` `:169`, no `proxy_cache_path`; no `_next` route in src/templates/static | grep |
| I5-e | STILL PRESENT | UPHELD | 0 `mem_limit\|cpus\|deploy:\|resources\|pids_limit\|ulimits` across 3 compose files; 7 services | grep |
| I5-f | STILL PRESENT (+worker) | UPHELD | `./prompts:/app/prompts` at prod `:41,97,126`; worker `:59-82` mounts only `./profiles`; `src/services/llm.py:38` `prompts/profile-synthesis.md` and `:86` `prompts/private-profile-synthesis.md` (FileNotFoundError fallback `:42`) reached via `worker/main.py:18 → profile_pipeline.py:26,310` | sed/grep |
| I5-g | CONFIRMED (incomplete) | UPHELD | `limit_conn`/`limit_req` only at `:107,112,130` (inside `${DOMAIN}` server `:71-175`); devel `:195-243` and blackbird `:268-307` have none; blackbird block lacks `ssl_ciphers`, `ssl_stapling*`, `resolver*` (present at `:83,92-95` and `:205,212-215`). Blackbird does keep `ssl_protocols TLSv1.2 TLSv1.3` + `ssl_prefer_server_ciphers off`, so "drops hardening" = cipher list + OCSP only, not protocol floor | sed/grep |
| I5-h | N/A | UPHELD | out of scope | — |
| P-1 | CONFIRMED (line drift) | UPHELD | `.gitignore:89-91` | sed |
| NEW-A (first agent) | `env.prod` not matched by `.env`/`.env.*` | UPHELD (mechanical) | see §2 Go output | — |
| NEW-B (first agent) | ratchet ceilings env-overridable; ruff unpinned | UPHELD | `${SRC_LINT_MAX:-260}`, `${COV_MIN:-60}`; `ruff>=0.4.0` `pyproject.toml:35` | — |
| NEW-C (first agent) | "could not confirm whether an image was built since 2026-08-10" | **OVERTURNED → CONFIRMED REALISED** | two local images inspected; see NEW-1/NEW-2 | `docker history`, `docker save` |

## 2. Detail — QUALIFIED / OVERTURNED / NEW

### I3-b / NEW-A — `.dockerignore` semantics (mechanical)

Docker (moby/patternmatcher, used by both the classic builder and BuildKit's dockerignore handling) matches each
pattern against the context-relative path **and each parent-directory prefix** with Go `filepath.Match` semantics;
`*`/`?` do not cross `/`; only the `**` extension spans directories. Docs: "Matching is done using Go's
filepath.Match rules … relative to the root of the context … Beyond Go's filepath.Match rules, Docker also supports
a special wildcard string `**`". So a bare `.env` excludes only `<ctx>/.env`; `.env.*` only `<ctx>/.env.<x>`;
nothing under `backups/` is touched. Confidence: high. Verified with `/usr/bin/go` (`scratchpad/27/m.go`,
path-or-parent loop over `filepath.Match`):

```
.env     backups/prod-sync-20260810/env.prod           match=false
.env.*   backups/prod-sync-20260810/env.prod           match=false
.env.*   backups/prod-sync-20260810/env.local-testing  match=false
.env     backups/x/.env                                match=false   (nested .env not caught either)
backups  backups/prod-sync-20260810/env.prod           match=true    (what the fix needs)
```
(`**/.env` prints false in this harness only because plain `filepath.Match` has no `**`; Docker rewrites `**`
to a regex — not relevant to the two patterns actually in the file.)

### NEW-1 — the leak is REALISED in a local image (blackbird), not latent

`docker images` shows two project images: `copiscience-app:latest` (2026-07-30, 1.22 GB, compose project
`copiscience`) and `copi-blackbird-agent:latest` (2026-08-21, 2.15 GB, labels
`com.docker.compose.project=copi-blackbird`, `service=agent`).

`docker history copi-blackbird-agent:latest` → `COPY . .` layer = **857 MB** (vs 2.89 MB for `COPY src/ src/`).
Listing that layer via `docker save … | tar -xOf - blobs/sha256/9edabb72… | python tarfile` (26,058 entries,
paths under `app/`):

```
438.01 MB  .venv-test
296.76 MB  backups          ← copi_pre0027_20260815 (72 MB), copi_pre0028_20260817 (72 MB),
                              copi_pre0030_20260820 (79 MB), copi_predeploy_20260814 (73 MB) pg dumps,
                              grantbot_posted_foas_*.sql, memory_1786106969/<agent>/public.md ×N
 20.96 MB  .git
  7.70 MB  tests
  5.52 MB  .superpowers
  5.14 MB  data
  2.30 MB  logs
  0.28 MB  .pytest_cache
  0.01 MB  .env             ← ('.env', 6329 bytes, mtime 2026-08-11 09:28)   NOT OPENED
           .env.example, SECOND_INSTANCE_SETUP.md, slack_install_links.md …
           NO .dockerignore entry in the layer
```
`.git`, `logs`, `data`, `.pytest_cache`, `.env` are all in the current `.dockerignore`, so that build context had
**no `.dockerignore` at all**. Cross-check: `git show remotes/origin/blackbird:.dockerignore` → "exists on disk,
but not in 'remotes/origin/blackbird'"; the dump names (`pre0027/0028/0030`) exceed this tree's head (0024), and
`SECOND_INSTANCE_SETUP.md` first appears in `c62af04` (2026-08-27, blackbird). So this image was built from the
**blackbird instance's tree** (the second prod instance, `/home/ubuntu/blackbird-copi-science` per
`docs/specs/2026-08-10-org1-parity-design.md:30`), which has never had a `.dockerignore`. Whether this exact
image ID is the one running on EC2 is unverifiable here (local ≠ prod host), but: (a) a 6.3 KB `.env` and four
prod-era pg dumps sit in an image layer on this machine today, and (b) every blackbird build shares that defect.
This is worse than the issue's copi-prod residual — on blackbird the `.env` pattern is not "insufficient", it is
absent.

### NEW-2 — `copiscience-app:latest` (2026-07-30) also baked `.env`

`COPY . .` layer 313 MB (17,045 entries): `.venv-test` 251 MB, `.git` 4.6 MB, `tests`, `mutants`, `.notes`,
`.coverage`, and **`.env` (512 bytes, mtime 2026-07-16)**. Pre-dates `.dockerignore` (08-03) and `backups/`
(08-10), so no `env.prod` in it; but it is the second local image with a `.env` inside. Neither image contains
`backups/prod-sync-20260810/env.prod` — that specific leak is still latent for the copi-prod tree (no local
build since 08-10 from this checkout), consistent with the first agent's hedge.

### I3-g — `.coverage`

`ls -la .coverage` → `-rw-r--r-- 1 a a 589824 Sep 2 08:33 .coverage`. It exists (a sibling agent's pytest-cov run
this morning most likely created it; the first agent's report is stamped 08:34). The issue's row is fully STILL
PRESENT; the first agent's "PARTIALLY" is a timing artefact, not an error in the issue.

### I5-g — blackbird "drops hardening": precise scope

Blackbird HTTPS block (`:268-307`) retains `ssl_protocols TLSv1.2 TLSv1.3`, `ssl_prefer_server_ciphers off`,
session cache/tickets settings and the same 4 `add_header`s; it omits **only** `ssl_ciphers`, `ssl_stapling`,
`ssl_stapling_verify`, `resolver`, `resolver_timeout`. Without `ssl_ciphers` nginx falls back to its default
(`HIGH:!aNULL:!MD5`), which is weaker than the explicit ECDHE-only list but not protocol-downgradeable. Verdict
stands; wording "drops hardening" should read "drops the cipher allow-list and OCSP stapling".

### I5-b — `/admin` timeout

No `location /admin` (or any admin-specific block) exists in `nginx/nginx.conf`; the only `location` directives
are `/`, `/.well-known/acme-challenge/`, the SEC-15 graph regex, `/ingest/*`, `/_next/static/`. Admin requests
fall through `location /` at `:128` → `proxy_read_timeout 120s` `:145`. Nothing per-location changes it.

## 3. Mis-cites in the first agent's report

- None material. Spot-checked: `ci.sh:4-5`, `:46`, `:55`, `:109-121`, `:210`, `:229-231`, `:261`, `:276-284`,
  `:293-295`; `main.py:150-153`; compose `:45-50`, `:162-164`, `:41/97/126`; `nginx.conf` all cited lines;
  `pyproject.toml:12-28,35,44,68-69`; `.gitignore:89-91`; `run_migration.sh:56,70,252-256,284-293`;
  `llm.py:38,42` (and `:86` uncited) — all exact.
- Factual slip: "`.coverage` does not exist on disk" — it does (589,824 B).
- Factual slip: "I could not confirm whether an image has actually been built since 2026-08-10" — it could be
  confirmed read-only via `docker history`; the answer is yes (blackbird, 08-21), with `.env` and dumps inside.

## 4. Counts

Rows evaluated: 43 (40 first-agent table rows + 3 first-agent NEW claims).
**UPHELD 41 · OVERTURNED 1 (NEW-C: image-build status was verifiable and is confirmed realised) · QUALIFIED 1
(I3-g: `.coverage` exists) · UNVERIFIABLE 0.**
Net effect on the issue: every I1–I5 defect verdict stands; I3's severity rises further (realised `.env` +
pg-dump bake in a blackbird-branch image; blackbird branch has no `.dockerignore` at all).
