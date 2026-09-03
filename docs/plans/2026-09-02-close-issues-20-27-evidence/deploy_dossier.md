# Deploy dossier — coPI.science prod (org1), branch copi-prod @ 18ba52c

Facts only. Every claim cites `file:line` in /home/a/scripps/coPI.science unless the path
is prefixed `MEMORY:` (operator memory notes under
`/home/a/.claude/projects/-home-a-scripps-coPI-science/memory/`, NOT repo content — treat as
unverified hearsay until re-measured on the host). Nothing was executed against Docker, SSH
or the network while compiling this.

Alembic head in this checkout is **0024** — no file has `down_revision = "0024"`
(`alembic/versions/` listing; `0024_add_agent_role.py:21-22`). Whether prod is at 0024 must be
read from the DB (`select * from alembic_version`, `docs/production-migration.md:521`).

---

## 1. Prod topology (`docker-compose.prod.yml` + `docker-compose.override.yml`)

Rule: **production is `docker-compose.prod.yml` + `docker-compose.override.yml`; always pass
both `-f` flags** (`CLAUDE.md:28-29`). Shortcut: `export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml`
(`CLAUDE.md:44`). Bare `docker compose` reads the **dev** file `docker-compose.yml` (no
`restart:`, `uvicorn --reload`, publishes 8001) (`CLAUDE.md:29-31`; `docker-compose.yml:20-22`).

Compose project name is `copi-python` — container names follow `<project>-<service>-1`:
`copi-python-app-1` (`CLAUDE.md:50`), `copi-python-postgres-1`
(`scripts/backup/backup.env.example:6`), `copi-python-worker-1` (`docs/inbound-email.md:93`),
`copi-python-certbot-1` (`scripts/ci.sh:170`). Project/path table: `docs/specs/2026-08-10-org1-parity-design.md:30`.

| service | image / build | command | restart | volumes | healthcheck | depends_on | profile | ports |
|---|---|---|---|---|---|---|---|---|
| postgres | `postgres:15` (`prod.yml:3`) | image default | `unless-stopped` (`:4`) | `pgdata:/var/lib/postgresql/data` (`:10`) | `pg_isready -U ${POSTGRES_USER:-copi}` 10s/5s/5 retries/start 10s (`:11-16`) | — | — | **none published** (no `ports:`; confirmed `docs/production-migration.md:34-35`) |
| app | `build: context: .` (`:26-27`) | `uvicorn src.main:app --host 0.0.0.0 --port 8000` (`:29`) | `unless-stopped` (`:28`) | `./profiles:/app/profiles`, `./prompts:/app/prompts` (`:40-41`) | `urllib.request.urlopen("http://127.0.0.1:8000/api/health")` 30s/10s/3/start 15s (`:45-50`) | postgres healthy (`:42-44`) | — | `expose: 8000` only (`:30-31`) |
| worker | `build: .` (`:60-61`) | `python -m src.worker.main` (`:63`) | `unless-stopped` (`:62`) | `./profiles:/app/profiles` only (`:71-72`) — **no prompts mount** | none | postgres healthy (`:73-75`) | — | — |
| agent | `build: .` (`:85-86`) | `python -m src.agent.main` (`:87`) | **none → `no`** (`:84-110` has no `restart:`; design doc says "restart=no by design" `docs/specs/2026-08-18-postgres-backup-verification-design.md:555`) | `./profiles`, `./prompts`, `./data` (`:95-98`) | none | postgres healthy (`:99-101`) | `profiles: [agent]` (`:102-103`) | — |
| grantbot | `build: .` (`:113-114`) | `python -m src.agent.grantbot scheduler --run-hour 8 --max-per-channel 1` (`:116`) | `unless-stopped` (`:115`) | `./profiles`, `./prompts`, `./data` (`:124-127`) | none | postgres healthy (`:128-130`) | — | — |
| nginx | `nginx:1.27-alpine` (`:140`) | see §6 (`:171`) | `unless-stopped` (`:141`) | `./nginx/nginx.conf:/etc/nginx/templates/default.conf.template:ro`, `./certbot/conf:/etc/letsencrypt:ro`, `./certbot/www:/var/www/certbot:ro` (`:156-159`) | `wget --no-check-certificate https://localhost/` 30s/5s/3/start 10s (`:165-170`) | app healthy (`:162-164`) | — | `80:80`, `443:443` (`:142-144`) |
| certbot | `certbot/certbot:latest` (`:181`) | entrypoint loop `certbot renew --quiet; sleep 12h` (`:186`) | `unless-stopped` (`:182`) | `./certbot/conf:/etc/letsencrypt`, `./certbot/www:/var/www/certbot` rw (`:183-185`) | none | — | — | — |

- Every app-family service: `env_file: .env` (`:32,64,88,117`) plus `environment:` overrides
  `DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER:-copi}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-copi}`,
  `SECRET_KEY: ${SECRET_KEY:?...}`, `ENVIRONMENT: ${ENVIRONMENT:-production}` (`:33-38,65-70,89-94,118-123`).
- Compose-parse-time required vars (`:?`): `POSTGRES_PASSWORD` (`:7`), `SECRET_KEY` (`:37,69,93,122`), `DOMAIN` (`:161`).
- nginx networks: `default` + external `copi-edge` (`:153-155`); `copi-edge` is created out-of-band
  with `docker network create copi-edge` and declared `external: true` (`:198-203`). nginx has
  `extra_hosts: host.docker.internal:host-gateway` for the devel vhost (`:145-148`).
- Log driver: `prod.yml` sets `logging.driver: awslogs` on every service (`:17-23` etc.);
  `docker-compose.override.yml:13-34` forces `json-file` for all seven services because the EC2
  role `copi-ec2-ses-role` lacks `logs:CreateLogStream` — without the override every container
  dies at start with `AccessDeniedException` (`override.yml:1-4`; `CLAUDE.md:36-39`).
- Verify correct file set: `docker inspect copi-python-app-1 -f '{{.HostConfig.RestartPolicy.Name}}'`
  → all six services must report `unless-stopped` (`CLAUDE.md:47-51`). (Six = everything but `agent`.)
- Only one volume: `pgdata` (`prod.yml:195-196`). Host volume name per design doc: `copi-python_pgdata`
  (`docs/specs/2026-08-18-postgres-backup-verification-design.md:596`; `scripts/backup/failure_injection.sh:180`).
- `stop_grace_period: 30s` exists **only in the dev file** (`docker-compose.yml:53`); prod agent has
  none, so `docker stop` without `-t 30` uses Docker's 10 s default.

### Image contents (`Dockerfile`, `.dockerignore`)
- `python:3.11-slim`; `COPY pyproject.toml . ; COPY src/ src/ ; RUN pip install --no-cache-dir .`
  (bakes `src/` into site-packages) then `COPY . .` (bakes the whole tree at `/app`) (`Dockerfile:1-17`).
  `RUN mkdir -p profiles/public profiles/private prompts logs static` (`:20`). Default CMD uvicorn (`:24`).
- `.dockerignore` excludes `.git`, `certbot`, `logs`, `data`, `*.log`, caches, `.venv`,
  `.provision_state.json`, **`.env` and `.env.*`** (`.dockerignore:1-17`). So `alembic/`,
  `scripts/`, `prompts/`, `profiles/`, `templates/` ARE baked into the image; profiles/prompts/data
  are then shadowed by bind mounts at runtime (`prod.yml:39-41,71-72,95-98,124-127`).
- Consequence: `import src` from `python scripts/X.py` resolves to the **site-packages copy**
  unless `PYTHONPATH=/app` is set (`scripts/migrate/run_migration.sh:99-105`;
  `docs/production-migration.md:341-345`).

---

## 2. How code reaches prod

- **No deploy script exists.** `scripts/` contains no deploy/pull/release script (`ls scripts/`);
  no server-side CI — `./scripts/ci.sh` run by the local `pre-push` hook is "the whole gate"
  (`CLAUDE.md:5-8`).
- Mechanism is manual: `git pull` on the host, then rebuild with compose. Documented sequence
  (`docs/issue-29-remediation.md:14-48`): on the prod host (`ssh ubuntu@copi.science`, repo
  `~/copi-python`) (`:14`) → "Merge/pull this branch;
  `export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml`" (`:16`) → stop agent
  gracefully (`:17-25`) → `docker compose up -d --build app worker && docker compose --profile agent build agent`
  (`:27-28`) → start agent (`:47-48`).
- Canonical rebuild commands (`CLAUDE.md:82-103`):
  ```bash
  C="-f docker-compose.prod.yml -f docker-compose.override.yml"
  docker compose $C up -d --build app worker
  docker compose $C --profile agent build agent
  ```
  "prod bakes code into the image, so skipping [`build agent`] silently runs whatever source was
  current at the last build" (`CLAUDE.md:98-100`, `:108-111`).
- Migration runbook's own deploy step: `docker compose up -d --build app worker`
  (`docs/production-migration.md:442`) — **bare `docker compose`**, no `-f` flags, and the runbook
  never mentions `COMPOSE_FILE` (grep of `docs/production-migration.md` and
  `scripts/migrate/run_migration.sh` for `COMPOSE_FILE|docker-compose.prod|override.yml` → no matches).
  `docs/specs/2026-08-10-org1-parity-design.md:418-420`: `run_migration.sh` "shells out to bare
  `docker compose`, which resolves `docker-compose.yml` — the dev stack.
  `COMPOSE_FILE=docker-compose.prod.yml` is not optional" (note: that line omits the override file;
  `CLAUDE.md:44` includes it).
- grantbot is not in any documented rebuild command (`CLAUDE.md:96`, `issue-29-remediation.md:28`,
  `production-migration.md:442` all say `app worker`). `docker compose up -d --build app worker`
  rebuilds the shared image but does not recreate `grantbot`; the design-doc restore runbook is the
  only place that stops/starts grantbot (`...backup-verification-design.md:547-548,573-574`).
- Prod host checkout path: `/home/ubuntu/copi-python` (`scripts/backup/copi-backup.service:3`;
  `scripts/backup/copi-backup-failure@.service:3`; `scripts/set_cohort_active.py:24`;
  `scripts/seed_cohorts.py:29`; `docs/specs/2026-08-18-postgres-backup-verification-design.md:547`;
  `docs/specs/2026-08-10-org1-parity-design.md:30`). User: `ubuntu` (`docs/issue-29-remediation.md:14`).
  Backup system runs "on the HOST as root under systemd" (`scripts/backup/copi_backup.py:6`).
- Docs also cite a `docker run` pattern for one-off scripts:
  `-v /home/ubuntu/copi-python:/work -w /work copi-python-app python ...` (`scripts/seed_cohorts.py:29-30`)
  — i.e. the built app image is tagged `copi-python-app`.
- **Second stack (blackbird)** — from `docs/specs/2026-08-10-org1-parity-design.md:28-38`:
  path `/home/ubuntu/blackbird-copi-science`, compose project `copi-blackbird`, domain
  `blackbird.copi.science` "proxied **by org1's nginx**", web service `blackbird-app` ("an
  *uncommitted* host-local compose edit; the tracked `docker-compose.prod.yml` says `app` on every
  branch"), star topology, `cohort_isolation_enabled=True`, alembic 0025 at the time of writing.
  org1's `prod.yml:149-152` comments: `copi-edge` is "the shared external network the second
  (blackbird) stack's app joins so nginx can proxy to blackbird-app:8000. See
  ../blackbird-copi-science/SECOND_INSTANCE_SETUP.md" (that file is not in this repo). Backup config
  lists both: `copi-python:copi-python-postgres-1:copi:copi` and
  `copi-blackbird:copi-blackbird-postgres-1:copi:copi` (`scripts/backup/backup.env.example:6-7`).
  blackbird's agent container is `blackbird-agent-run` (`...backup-verification-design.md:553`).
  **Deploying org1 does not touch blackbird's containers, but recreating org1's nginx affects
  blackbird's edge** (nginx.conf vhost, §6).
- MEMORY (not repo): `org1-prod-state-2026-08-14.md:19` — "a daily 07:00 cron runs
  `claude -p --permission-mode auto` inside the prod repo (don't deploy across 07:00 blindly)".
  Repo corroboration that a 07:00 UTC Claude cron exists on the host:
  `docs/specs/2026-08-18-postgres-backup-verification-design.md:470-473` ("the 07:00 UTC
  `daily_audit.md` Claude cron … runs for roughly 8 minutes").
- README.md `Running locally` (`README.md:36-47`) and AGENT.md (`AGENT.md:141-146`) are **dev**
  instructions (bare `docker compose`, `alembic upgrade head`); README:99 still references
  `PILOT_LABS`, which `CLAUDE.md:121-122` says no longer exists.

---

## 3. Schema migrations in prod today

### Is anything automatic?
**No.** `grep -rn create_all src/` → nothing; `src/main.py` and `src/database.py` have no
lifespan/startup/alembic hooks (grep for `lifespan|startup|on_event|create_all|alembic|migrat` →
no matches); `src/worker/main.py:178-181` is `asyncio.run(run_worker())`; prod app command is bare
uvicorn (`prod.yml:29`). Confirmed in prose: "Nothing migrates automatically: the prod web command
is a bare `uvicorn` and there is no `create_all`" (`docs/specs/2026-08-10-org1-parity-design.md:422-423`).
The only automatic `alembic upgrade head` calls are test/CI-side (`tests/conftest.py:63-73`,
`scripts/ci.sh:229-231`).

### How alembic finds the DB
- `alembic.ini:5` hardcodes `sqlalchemy.url = postgresql+asyncpg://copi:copi@localhost:5432/copi`.
- `alembic/env.py:20-23`: `db_url = os.environ.get("DATABASE_URL"); if db_url: config.set_main_option("sqlalchemy.url", db_url)`.
  So **only `DATABASE_URL` overrides the localhost default**; app-side `settings.database_url`
  (`src/config.py:111`, `src/database.py:16-18`) is NOT consulted by alembic.
- Inside prod containers `DATABASE_URL` is injected by compose (`prod.yml:34` etc.), so
  `docker compose exec app python -m alembic ...` targets the right DB. On the **host**, the
  bare hostname `postgres` resolved to a public IP (195.35.25.84) via a LAN search domain on the
  dev machine (`docs/production-migration.md:33-39`; `run_migration.sh:133-140`).
- `alembic/env.py:78-81`: `do_run_migrations` calls `context.configure(connection=..., target_metadata=...)`
  without `transaction_per_migration` → **the entire chain is one transaction** (`env.py:48-54`;
  `production-migration.md:71-74`).
- `ALEMBIC_LOCK_TIMEOUT_MS` (default `10000`) applied as asyncpg `server_settings.lock_timeout`
  at connect (`env.py:66,88-93`); `0` = wait forever (`production-migration.md:206`). It bounds lock
  *wait*, not statement duration (`env.py:64-65`).
- Known silent-failure mode: executing SQL on the connection before `context.begin_transaction()`
  makes every migration log "Running upgrade" then roll the whole chain back with **no**
  `alembic_version` row (`env.py:69-77`; `production-migration.md:41-44`). Hence "read the revision
  back" (`run_migration.sh:268-293`).

### `scripts/migrate/run_migration.sh` — full flow
- Defaults: `TARGET="0024"` (`:56`; header comment `:3` still says "head 0023" — stale),
  `DSN="${DATABASE_URL:-}"` (`:57`), `BACKUP_DIR="${MIGRATE_BACKUP_DIR:-backups}"` (`:58`),
  `SVC="${MIGRATE_SERVICE:-app}"` (`:59`), `PG_SVC="${MIGRATE_PG_SERVICE:-postgres}"` (`:60`),
  `LOCK_TIMEOUT_MS="${ALEMBIC_LOCK_TIMEOUT_MS:-10000}"` (`:61`).
- Flags: `--apply`, `--target <rev>`, `--database-url <dsn>`, `--backup-dir <path>`,
  `--backup-verified-elsewhere "<reason>"`, `-h/--help`; `--skip-backup-check` is rejected loudly
  (`:67-88`). **Default is rehearsal** — writes nothing (`:11-17`).
- Exit codes: `0` clear/applied, `1` BLOCKED, `2` rehearsal warnings, `3` operational, `64` usage (`:28-35`).
- `cd "$REPO_ROOT"` (`:52-53`); every compose call is **bare `docker compose`** (`:109,114,123,152,182,189,194-196,254`)
  → `COMPOSE_FILE` must be exported for the prod file set (see §2).
- **Step 1** (`:107-128`): requires service `$SVC` running (`docker compose ps --status running --services`),
  else exit 3 with hint `docker compose up -d --build app`; asserts
  `docker compose exec -T -e PYTHONPATH=/app app python -c 'import src; print(src.__file__)'` ==
  `/app/src/__init__.py`, and `from src.models import Cohort` imports (`:123-127`). **Implication:
  the running `app` container must already carry the new source (i.e. be rebuilt from the branch
  that contains the migrations) before this script can run.** The runbook reconciles this with
  "migrate before code" by noting old code keeps working on the new schema because new columns have
  defaults (`production-migration.md:445-448`); MEMORY `org1-prod-access-and-local-copy.md:24` notes the
  new web app "starts healthy against 0018".
- **Step 2** (`:142-149`): exit 64 if no DSN; prints DSN with password masked.
  `run_py()` = `docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL="$DSN" "$SVC" python "$@"` (`:151-153`).
- **Step 3 backup** (`:160-204`), *before* preflight: if `--backup-verified-elsewhere` → WARN and pass
  the reason to preflight; if rehearsal → prints would-dump and passes
  `--backup-verified-elsewhere "rehearsal mode — no dump taken"`; if `--apply`:
  `DBNAME` parsed from DSN (`:171`); file
  `$BACKUP_DIR/${DBNAME}_pre${TARGET}_$(date +%Y%m%dT%H%M%S).dump` (`:172`);
  `docker compose exec -T postgres pg_dump -U copi -Fc -f /tmp/copi_migrate_$$.dump "$DBNAME"` (`:181-182`);
  verify TOC in-container `pg_restore -l` (`:189`); count TOC entries (`:194`);
  `docker compose cp postgres:/tmp/... "$BACKUP_FILE"` (`:195`); rm temp; host size must be ≥1024 bytes (`:197-201`);
  passes `--backup-path "$BACKUP_FILE"` to preflight (`:203`). **Hardcodes `-U copi`** (`:182`).
- **Step 4 preflight** (`:209-226`): snapshot path `SNAP="${MIGRATE_SNAPSHOT:-$BACKUP_DIR/preflight_snapshot.json}"` (`:211`);
  `run_py scripts/migrate/preflight.py --target "$TARGET" --snapshot "$SNAP" "${EXTRA_PREFLIGHT[@]}"` (`:214-215`).
  Exit 0 PASS, 2 WARN (continues), else BLOCKED exit 1 with remediate_duplicates hint (`:218-226`).
  Rehearsal stops here: exit 2 if warnings, else 0 (`:228-245`).
- **Step 5** (`:251-266`): `docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL="$DSN" -e ALEMBIC_LOCK_TIMEOUT_MS="$LOCK_TIMEOUT_MS" app python -m alembic upgrade "$TARGET"`;
  non-zero → exit 1 with hints (`LockNotAvailableError` → `docker stop -t 30 agent-run` and re-run).
- **Step 6** (`:276-293`): reads `select version_num from alembic_version` via SQLAlchemy inside the
  container; must equal `$TARGET`, else "Treat this as a silent rollback. Do NOT deploy code." exit 1.
- **Step 7** (`:299-310`): `run_py scripts/migrate/postflight.py --target "$TARGET" --snapshot "$SNAP"`;
  non-zero → exit 1, "Do NOT deploy application code."
- Footer prints steps 8–10 (backfill_slack_ts report/apply; deploy code + restart app+worker; start
  agent-run last) (`:312-326`).
- What it deliberately does NOT do: stop/start containers, run `backfill_slack_ts.py`, resolve
  duplicates (`:37-47`).

### Preflight (`scripts/migrate/preflight.py`) assumptions
- `DEFAULT_TARGET = "0024"` (`:74`); `SUPPORTED_START_REVISIONS = ("0018", "0019", "0020", "0021", "0023")` (`:89`)
  — note **0022 is not a supported start, and 0024 is not a supported start** (tooling is specific to
  the 0018→0024 chain; `REVISION_ORDER` ends at `"0024"` `:206`). Docs list only 0018/0019/0020/0021
  (`production-migration.md:7`); code comment explains 0023 (`preflight.py:75-77`).
- 13 checks (`production-migration.md:281-295`): 1 supported start rev; 2 single head/no dup ids;
  3 the 0019 stamp is the *content* 0019 (three historical 0019s: `production-migration.md:297-323`);
  4 no duplicate `(simulation_run_id, message_ts)`; 5 planned objects don't pre-exist; 6 rows blocking
  downgrade (`agent_id IS NULL`); 7 blocking sessions (idle-in-transaction always BLOCKs; active xact
  older than `--max-xact-age-s` 5.0 BLOCKs, `preflight.py:471,1883-1888`); 8 env.py harness commits;
  9 sizing/lock-window estimate; 10 disk headroom; 11 legacy-row inventory (WARN); 12 recent
  non-trivial backup (default max age 24h `:138`, min 1024 bytes `:143`, searched in
  `("backups", "data/backups", "/backups", "/var/backups/copi")` `:148`, globs `:149`; WARN in
  rehearsal); 13 row-count snapshot written.
- Exit: `0` ok, `1` BLOCKED, `2` warnings (`:70-72`). CLI flags `:1826-1889`.
- Postflight (`scripts/migrate/postflight.py`) 13 checks (`production-migration.md:373-387`);
  `EXPECTED_TABLES = ("pi_dm_messages","cohorts","cohort_memberships","cohort_audit_events")` (`:132`);
  hardcoded expected columns/indexes/constraints/enums for the 0019–0024 chain (`:97-180`).
  "Postflight must be 0 FAIL before you deploy code" (`production-migration.md:393`).

### Runbook order (`docs/production-migration.md`)
Five hard rules `:21-44`: backup+verify; **migrate DB BEFORE deploying code**; `alembic downgrade`
is not a rollback; never let DSN default / always inside container; alembic output is not evidence.
Steps: §2 read-only measurement Q1–Q4 (`:88-135`), §6a rehearse
`export DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi; ./scripts/migrate/run_migration.sh` (`:263-266`),
§6b `./scripts/migrate/run_migration.sh --apply` (`:335-337`), §8 step 8 backfill_slack_ts (`:422-424`),
step 9 `docker compose up -d --build app worker` (`:441-442`), step 10
`docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0` (`:459-460`).
Lock-timeout hit → `docker stop -t 30 agent-run` then re-run (`:190-198`);
`ALEMBIC_LOCK_TIMEOUT_MS=30000 ./scripts/migrate/run_migration.sh --apply` to raise (`:202-204`).
Duplicates: `remediate_duplicates.py` dry-run / `--apply`, strategies, exit codes (`:226-255`).
Quick reference table (`:544-558`). Untested: >2.5 M rows, restore drill did not start the app,
managed Postgres, replication (`:586-597`).

---

## 4. Backups

### Nightly verified backup (host-level, `scripts/backup/`)
- Runs on the host as root under systemd; talks to DBs only via `docker exec`; every dump is
  restored into a throwaway `--network none`, memory-capped `postgres:15` container and per-table
  row counts compared against the dump's own snapshot (`copi_backup.py:2-11`; design §4.2/§4.4
  `...design.md:134-196,224-309`).
- Install: `sudo scripts/backup/install.sh` → `/usr/local/bin/copi-backup`, `/etc/copi-backup/backup.env`
  (0600, from `backup.env.example` if absent), `/var/backups/copi` (0700), five units into
  `/etc/systemd/system/`, `systemctl daemon-reload`; does NOT enable timers (`install.sh:16-40`).
  Enable: `systemctl enable --now copi-backup.timer copi-backup-report.timer` (`install.sh:40`).
- Units: `copi-backup.service` `ExecStart=/usr/local/bin/copi-backup run`, `TimeoutStartSec=3600`,
  `Nice=10`, `IOSchedulingClass=idle`, `ProtectSystem=strict`, `ReadWritePaths=/var/backups /run`,
  `OnFailure=copi-backup-failure@%n.service` (`copi-backup.service:10-20`);
  `copi-backup.timer` `OnCalendar=*-*-* 01:00:00 America/Los_Angeles`, `Persistent=true` (`copi-backup.timer:5-6`);
  weekly `copi-backup-report.timer` `Mon *-*-* 08:00:00 America/Los_Angeles` (`copi-backup-report.timer:5`)
  → `copi-backup report` (`copi-backup-report.service:7`); failure unit runs `copi-backup report`
  (`copi-backup-failure@.service:12`).
- Config keys (`backup.env.example:6-38`): `STACKS` (`stack:container:db:user` per line — org1 =
  `copi-python:copi-python-postgres-1:copi:copi`), `BACKUP_ROOT=/var/backups/copi`,
  `RETENTION_COUNT=5`, `RETENTION_UNVERIFIED=2`, `VERIFY_IMAGE=postgres:15`, `VERIFY_MEM=768m`,
  `VERIFY_TIMEOUT_SEC=1800`, `FREE_SPACE_FACTOR=7`, `REGRESSION_TOLERANCE_PCT=20`, `OFFSITE_CMD=""`,
  `AWS_REGION`, `SES_SENDER_EMAIL`, `MAIL_TO`.
- Where dumps land / naming: `/var/backups/copi/<stack>/<stack>_<db>_YYYYMMDDTHHMMSSZ.dump`
  + `.json` sidecar; failed verify → `.dump.unverified` (`copi_backup.py:192-216`;
  `_cmd_run_inner` `:1321-1344`; design `:73-76`). For org1: `/var/backups/copi/copi-python/copi-python_copi_<ts>.dump`.
  `status.json` at `/var/backups/copi/status.json` (`:1226`; design `:76`).
- Retention: count-based — keep 5 newest verified + 2 newest unverified per stack; **never prune a
  stack with zero verified dumps**; sidecar goes with dump (`copi_backup.py:234-256`; design `:316-352`).
  Pruner regex is anchored so `run_migration.sh` dumps (`copi_pre0024_...dump`, no `T`/`Z`) are
  unreachable (`copi_backup.py:194-201`).
- Flow: flock `/run/copi-backup.lock` — **exits 0 silently if already held** (`:1083,1141-1144`);
  sweep leftovers (label-filtered, never `docker volume prune`) (`:1102-1129`; design `:118-127`);
  free-space guard `free >= FREE_SPACE_FACTOR × last dump` (`:1292-1315`); per stack: REPEATABLE READ
  snapshot session → `pg_dump -Fc --snapshot` into container `/tmp` → `pg_restore -l` in container →
  `docker cp` to `.partial` → fsync → atomic rename (`:686-720`; design `:134-196`); verify
  (`:735-800`); regression check vs previous sidecar (`:1264-1283`); write status; mail on failure;
  prune last (`:1355-1380`).
- CLI (`copi_backup.py:1485-1530`): `copi-backup run [--no-prune]`, `copi-backup report`,
  `copi-backup prune [--dry-run]`, `--config` default `/etc/copi-backup/backup.env`.
  **`run` has no dry mode** — `run --dry-run` is rejected (`:1489-1506`).
- **Manual pre-deploy dump via the nightly system**: `sudo /usr/local/bin/copi-backup run --no-prune`
  (dumps + verifies both stacks, ~800 MB, ~4 min per `...design.md:645-647`; if the timer's run
  holds the lock it exits 0 having done nothing `:1141-1144`). Under systemd:
  `systemctl start copi-backup.service` is not stated in docs; the design doc records "two full runs
  under systemd (`Result=success`)" (`...design.md:657-658`).
- Health check of the nightly (`...design.md:659-663`):
  ```bash
  systemctl status copi-backup.service
  journalctl -u copi-backup.service --since yesterday
  sudo jq '{last_run_utc,last_success_utc,ok}' /var/backups/copi/status.json
  ```
- Host has **no postgres client tools**; read a TOC with the image:
  `docker run --rm --network none -v <dump>:/d.bin:ro postgres:15 pg_restore -l /d.bin`
  (`scripts/backup/failure_injection.sh:33-38`; `copi_backup.py:753-764`; design `:227-229`).
  TOC check only sees the first ~755 KB; full restore is the real proof (design `:233-253`).
- Limitations: same volume as pgdata; 24h RPO; no PITR ("The pre-deploy dumps in
  `run_migration.sh` partially cover this case"); verify ≠ app correctness; unencrypted at rest
  (`...design.md:594-617`). `OFFSITE_CMD` never exercised (`:653-654`).

### Pre-deploy dump alternatives
- `run_migration.sh --apply` Step 3 (see §3): `backups/<db>_pre<TARGET>_<ts>.dump`, TOC-verified in
  container, copied out, size-checked (`run_migration.sh:170-203`). `backups/` is gitignored
  (`.gitignore:91`; `production-migration.md:554`).
- Design-doc manual snapshot (restore runbook step 2):
  `docker exec copi-python-postgres-1 pg_dump -Fc -U copi -d copi > /var/backups/copi/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).dump`
  (`...design.md:559-560`). (Streams via stdout to a host file — fine for writing; only
  `pg_restore -l` over a pipe fails, `run_migration.sh:174-180`.)

### Restore commands
- Runbook §9 (`docs/production-migration.md:523-538`):
  ```bash
  docker stop -t 30 agent-run || true
  docker compose stop app worker
  docker compose cp backups/copi_pre0023_<timestamp>.dump postgres:/tmp/restore.dump
  docker compose exec -T postgres psql -U copi -d postgres -c 'ALTER DATABASE copi RENAME TO copi_failed_migration'
  docker compose exec -T postgres psql -U copi -d postgres -c 'CREATE DATABASE copi'
  docker compose exec -T postgres pg_restore -U copi -d copi --exit-on-error /tmp/restore.dump
  docker compose exec -T postgres psql -U copi -d copi -c 'select * from alembic_version'
  ```
  "Rename rather than drop"; "`--exit-on-error` is not optional" (`:536-538`).
- Design doc §11 (`...design.md:544-575`): `cd /home/ubuntu/copi-python`; stop `app worker grantbot`
  with both `-f` flags; `docker stop agent-run` explicitly (one-off container, not in the compose
  invocation) (`:546-556`); pre-restore dump (`:558-560`);
  `psql -U copi -d postgres -c "DROP DATABASE copi WITH (FORCE);" -c "CREATE DATABASE copi OWNER copi;"` (`:562-564`);
  `docker exec -i copi-python-postgres-1 pg_restore --no-owner --no-privileges --exit-on-error -U copi -d copi < "$DUMP"` (`:566-568`);
  `\dt`; `docker compose ... start app worker grantbot` (`:570-574`). Warning: a dump older than
  the running image's schema needs migrations re-applied before starting writers (`:577-583`).
  "never pass `--profile agent`" when bringing things back (`:554-556`).

---

## 5. agent-run lifecycle

- One-off container named `agent-run` (`CLAUDE.md:55`). Start variants (`CLAUDE.md:58-70`):
  ```bash
  C="-f docker-compose.prod.yml -f docker-compose.override.yml"
  docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0
  docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --budget 50
  docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --fresh --budget 0
  docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --max-runtime 60 --budget 0
  ```
- Restart procedure, verbatim (`CLAUDE.md:79-104`):
  ```bash
  C="-f docker-compose.prod.yml -f docker-compose.override.yml"
  # 1. Save logs
  docker logs agent-run > logs/run_$(date +%s).log 2>&1
  ls -t logs/run_*.log | tail -n +11 | xargs rm -f
  # 2. Stop GRACEFULLY
  docker stop -t 30 agent-run
  docker rm agent-run
  # 3. Rebuild app + worker
  docker compose $C up -d --build app worker
  # 4. Rebuild the agent image too
  docker compose $C --profile agent build agent
  # 5. Start the new run
  docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0
  ```
- Graceful-stop requirement: "`docker rm -f` sends SIGKILL, which skips the shutdown flush and
  permanently loses the in-flight turn's messages (the DB, not Slack, is the durable store).
  `docker stop` sends SIGTERM; -t 30 leaves room for an in-flight LLM call to finish"
  (`CLAUDE.md:88-91`; `production-migration.md:193-198`). Code: SIGTERM/SIGINT handler sets a stop
  flag (`src/agent/main.py:238-250`); final flush `await sim_engine.stop()` runs in `finally` on
  every exit path (`:271-279`). No `stop_grace_period` in prod (§1) → `-t 30` must be explicit.
- In-flight state: buffered messages/LLM logs are flushed to Postgres on stop (`main.py:272-275`);
  a SIGKILL loses them. On resume the sim fetches Slack history per bot; expect ~10 min of
  `[<agent>] Rate limited, retrying in 10s (attempt 1/3)` before `=== Turn 1 ===`; repeated
  `attempt 1/3` is progress (`CLAUDE.md:73-77`; log string `src/agent/slack_client.py:333`).
- `--budget N>0` is deprecated: cumulative cap rebuilt from `llm_call_logs` benches agents
  permanently; use `--budget 0` (`src/agent/main.py:254-261`).
- Roster changes need no restart — `_sync_roster_from_db` every `ROSTER_POLL_INTERVAL = 30.0` s
  (`src/agent/simulation.py:149,4513-4520`; `CLAUDE.md:113-116`). **Code** changes do need a
  restart + `build agent` (`CLAUDE.md:108-111`; `production-migration.md:467-468`).
- Start agent-run **last**, after app+worker are on new code (`production-migration.md:457-465`).
- Never `--remove-orphans` (`CLAUDE.md:106`).
- **grantbot**: `restart: unless-stopped` (`prod.yml:115`); scheduler is a `while True: … time.sleep(check_interval)`
  loop with `asyncio.run(run_grantbot(...))` once per day when `now.hour >= run_hour` (08 UTC)
  (`src/agent/grantbot.py:748-786`); **no signal handler** in that module (grep `signal|SIGTERM` →
  none) → `docker stop` kills it at Docker's default 10 s. State file `data/grantbot_last_run.txt`
  (`grantbot.py:704`) on the `./data` bind mount (`prod.yml:127`) survives recreation;
  `_mark_run_complete()` runs only after a successful daily run (`:772-777`), and "If the container
  starts after the scheduled hour, it runs immediately to catch up" (`:758-759`) — recreating
  grantbot after 08:00 UTC before its daily run has completed triggers an immediate run. No doc
  imposes a graceful-stop constraint on grantbot; the design-doc restore runbook simply
  `stop`s/`start`s it with app and worker (`...design.md:547-548,573-574`).
- **worker**: inbound-email rate limiter and quarantine counters are in-memory and reset on restart
  "by design" (`docs/inbound-email.md:106-107`).

---

## 6. nginx

- Config generation: `./nginx/nginx.conf` is mounted read-only at
  `/etc/nginx/templates/default.conf.template` (`prod.yml:157`); the stock nginx image's
  `/docker-entrypoint.d/20-envsubst-on-templates.sh` substitutes `${DOMAIN}` and writes
  `/etc/nginx/conf.d/default.conf` (`nginx/nginx.conf:4-8`). Because the service overrides
  `command:`, the entrypoint script is invoked explicitly:
  `command: "/bin/sh -c '/docker-entrypoint.d/20-envsubst-on-templates.sh && (while :; do sleep 6h & wait $${!}; nginx -s reload; done &) && nginx -g \"daemon off;\"'"`
  (`prod.yml:171`). `DOMAIN` comes from `.env` (`prod.yml:160-161`, `:?` required).
  **`${DOMAIN}` is substituted only in the first two server blocks** (`nginx.conf:55,75,78-79`); the
  devel and blackbird vhosts are literal hostnames (`:180,199,257,272`).
- Reload without downtime: the mechanism the container itself uses is `nginx -s reload`
  (`prod.yml:171`), fired every 6 h. A manual reload is
  `docker exec copi-python-nginx-1 nginx -s reload` (container name by compose convention; not
  written in any repo doc — MEMORY `nginx-stale-upstream-ip-after-app-recreate.md:20-22` records it
  and suggests `nginx -t` first). Note: `nginx -s reload` re-reads the already-rendered
  `/etc/nginx/conf.d/default.conf`; **changing `nginx/nginx.conf` or `DOMAIN` requires recreating the
  container** so envsubst runs again (`prod.yml:171` runs the template step only at start).
- Upstreams: `upstream app { server app:8000; }` (`nginx.conf:38-40`) — a **static** upstream;
  `upstream devel_app { server host.docker.internal:8858; }` (`:45-47`);
  `upstream blackbird_app { server blackbird-app:8000; }` (`:249-251`). The `resolver 8.8.8.8 8.8.4.4`
  directives (`:94,214`) are for OCSP stapling in the server blocks.
- Three vhosts, each an HTTP→HTTPS redirect + ACME block and an HTTPS block:
  1. `${DOMAIN}` — `:52-66` (80) and `:71-172` (443): proxies `/` to `http://app` with rate limits
     (`limit_req zone=req_general burst=40`), tighter `req_graph` for
     `^/(cabo-graph|scripps-graph|schultz-alumni-pilot|schultz-group-alumni)$` (`:111-125`),
     PostHog proxies `/ingest/static/`, `/ingest/` (`:153-164`), `/_next/static/` cache (`:167-171`).
     Certs `/etc/letsencrypt/live/${DOMAIN}/{fullchain,privkey}.pem` (`:78-79`).
  2. `devel.copi.science` — `:177-189` / `:195-243`: proxies to `devel_app` (host:8858 via
     `host.docker.internal`); uses the `copi.science` SAN cert (`:201-202`).
  3. `blackbird.copi.science` — `:254-265` / `:268-307`: proxies to `blackbird_app` over `copi-edge`;
     certs `/etc/letsencrypt/live/blackbird.copi.science/` (`:274-275`).
- Rate-limit zones/http-context directives at top (`:29-35`); `limit_req_status 429`.
- Certs live on the host at `./certbot/conf` (→ `/etc/letsencrypt`) and ACME webroot `./certbot/www`
  (`prod.yml:158-159,184-185`); `certbot/` is gitignored (`.gitignore:59`) and absent from this
  checkout (`ls certbot/` → no such directory) — host-only state. Renewal loop: `certbot renew --quiet`
  every 12 h (`prod.yml:186`). `copi-python-certbot-1`'s volume is anonymous (`scripts/ci.sh:170-171`).
- nginx `depends_on: app: condition: service_healthy` (`prod.yml:162-164`): nginx cannot start
  until app is healthy; if app is absent nginx crash-loops on `host not found in upstream "app:8000"`
  (`CLAUDE.md:33-34`).
- MEMORY (`nginx-stale-upstream-ip-after-app-recreate.md:8-29`): recreating `app` gives it a new
  bridge IP; the static upstream keeps the old IP → site-wide 502 until `nginx -s reload` (up to 6 h
  self-heal via `prod.yml:171`). Fingerprint: nginx healthcheck `unhealthy` while app `healthy`.
  Observed 2026-08-18, ~65 min of 502s. Repo-side mechanism confirmed at `nginx.conf:38-40` and
  `prod.yml:171`; the incident itself is memory, not repo.

---

## 7. Post-deploy health / verification commands

- App health route: `GET /api/health` → `{"status": "ok"}` (`src/main.py:150-153`); used by the
  compose healthcheck (`prod.yml:46`). Through nginx: `https://<DOMAIN>/api/health`.
- Compose/Docker state: `docker compose $C ps` (healthchecks: postgres `prod.yml:11-16`, app
  `:45-50`, nginx `:165-170`); restart policy check
  `docker inspect copi-python-app-1 -f '{{.HostConfig.RestartPolicy.Name}}'` → `unless-stopped`
  for all six (`CLAUDE.md:47-51`).
- Migration state: `docker compose exec -T postgres psql -U copi -d copi -c 'select * from alembic_version'`
  (`production-migration.md:521`); `docker compose exec app alembic heads` must print one line
  (`README.md:45`); `alembic current` (`README.md:47`). Read-only Q1–Q4 SQL (`production-migration.md:94-124`);
  disk `docker compose exec -T postgres df -h /var/lib/postgresql/data` (`:132`).
- `run_migration.sh` Step 1 self-test that the container runs current code:
  `docker compose exec -T -e PYTHONPATH=/app app python -c 'import src; print(src.__file__)'` →
  `/app/src/__init__.py` (`run_migration.sh:114-117`).
- Admin pages (all under `/admin`, `src/main.py:146`): `/admin/users`, `/admin/users/{id}`,
  `/admin/jobs`, `/admin/activity`, `/admin/activity/{run_id}`, `/admin/activity/{run_id}/llm-calls`,
  `/admin/discussions`, `/admin/agents`, `/admin/agents/{id}`, `/admin/access-requests`,
  `/admin/waitlist`, `/admin/cohorts`, `/admin/cohorts/topology`, `/admin/cohorts/{id}`
  (`src/routers/admin.py:76,174,235,277,326,402,507,786,862,1151,1316,1475,1550,1717`).
  Agent provisioning UI at `/admin/agents → Provision → Approve & Activate` (`CLAUDE.md:144-150`).
- Agent log greps (`docs/issue-29-remediation.md:51-65`):
  ```bash
  docker logs agent-run 2>&1 | grep -iE "Rejected (draft|reply to thread)|Suppressed post to|stripped ungrounded"
  docker logs agent-run 2>&1 | grep -i "publication-record load failed"   # MUST be empty
  ```
  plus per-agent grouping `grep -oE "^\[[a-z]+\]" | sort | uniq -c`. Resume progress: `=== Turn 1 ===`
  and `Rate limited, retrying` lines (`CLAUDE.md:73-77`).
- Worker/inbound email: `docker logs -f copi-python-worker-1` for `Email review created`
  (`docs/inbound-email.md:91-94`); `python scripts/setup_inbound_email.py --check` (admin AWS creds)
  (`inbound-email.md:69-71`).
- Backup system: `systemctl status copi-backup.service`, `journalctl -u copi-backup.service --since yesterday`,
  `sudo jq '{last_run_utc,last_success_utc,ok}' /var/backups/copi/status.json` (`...design.md:659-663`);
  `systemctl --failed` (`:404`).
- Roster sync confirmation: INFO line fires on change (`src/agent/simulation.py:381`).

---

## 8. Rollback

### Schema (documented)
- "`alembic downgrade` is not a rollback. It either destroys data silently or refuses to run. Your
  rollback is a restore from the dump" (`production-migration.md:27-29`, §9 `:472-541`).
  Measured: with no `agent_id IS NULL` rows `alembic downgrade 0018` exits 0, keeps row count,
  and drops `content`, `pi_dm_messages`, `cohorts` (`:478-491`); with any PI row it fails
  `NotNullViolationError` on `ALTER TABLE agent_messages ALTER COLUMN agent_id SET NOT NULL` and
  rolls back cleanly (`:493-507`). Door-closing analysis: after new code runs,
  `_rebuild_state_from_slack` writes `agent_id=NULL` rows and downgrade past 0019 stops being
  possible (`docs/specs/2026-08-10-org1-parity-design.md:428-435`).
- If postflight fails: do not deploy code; nothing half-applied (one transaction); restore per §4
  (`production-migration.md:511-541`).

### `downgrade()` per revision (all six have a body)
| rev | downgrade | guarded? | notes |
|---|---|---|---|
| 0019 | drops 3 indexes, `uq_agent_messages_run_ts`, sets `agent_id` NOT NULL, drops 7 columns (`0019_agent_message_content.py:73-85`) | **no** `if_exists` | fails if any `agent_id IS NULL`; otherwise silently destroys message bodies |
| 0020 | drops 2 indexes, `pi_dm_messages`, enum `pi_dm_direction_enum` with `checkfirst=True` (`0020_pi_dm_messages.py:62-66`) | enum only | drops all PI DMs |
| 0021 | drops `ix_pi_dm_run_direction_created`, `ix_agent_messages_run_created` (`0021_...py:37-39`) | no | index-only, non-destructive |
| 0022 | drops 4 indexes + 3 tables, all `if_exists=True` (`0022_add_cohorts.py:126-149`) | yes | drops all cohorts/memberships/audit |
| 0023 | drops 3 columns `if_exists=True` (`0023_...py:59-62`) | yes | loses provenance values |
| 0024 | `op.drop_column("agents", "role", if_exists=True)` (`0024_add_agent_role.py:34-35`) | yes | loses role assignments |

CI proves the chain round-trips upgrade→downgrade→upgrade on a throwaway DB
(`scripts/ci.sh:229-231`; `docs/specs/2026-08-10-org1-parity-design.md:439-441`).

### Code / image rollback
**Not covered by any document.** No doc describes checking out a previous commit and rebuilding, image
tagging, or keeping the previous image. Images are built from the working tree each time
(`prod.yml:26-27`, `Dockerfile:17`) and are untagged beyond compose's default `copi-python-app`
(`scripts/seed_cohorts.py:30`). The only stated directional safety is "old code keeps working against
the new schema" (`production-migration.md:445-448`) with one known gap (private-channel close marker
not mirrored, `:450-455`).

### nginx / certs rollback
Not documented. Config is a tracked file (`nginx/nginx.conf`) rendered at container start (§6).

---

## 9. Environment variables (names only) and where they live

- **Location**: `.env` in the repo root on the host (`/home/ubuntu/copi-python/.env`), consumed via
  `env_file: .env` (`prod.yml:32,64,88,117`) and by compose interpolation for `${POSTGRES_PASSWORD}`,
  `${SECRET_KEY}`, `${DOMAIN}`, `${ENVIRONMENT}`, `${AWS_REGION}`, `${POSTGRES_USER}`, `${POSTGRES_DB}`
  (`prod.yml:6-8,23,34-38,161`). `.env` is gitignored (`.gitignore:20`) and dockerignored
  (`.dockerignore:16-17`). pydantic also reads `.env` directly (`src/config.py:99`,
  `SettingsConfigDict(env_file=".env", extra="ignore")`). `docs/inbound-email.md:84-90`: changing
  `.env` requires `up -d` to recreate — `docker restart` re-runs the OLD environment.
- **Backup system env**: `/etc/copi-backup/backup.env` (host, root 0600) (`install.sh:18-21`;
  design `:71`), deliberately duplicating SES settings so an app `.env` change cannot break backups
  (`...design.md:105-107`).
- **Compose-level names**: `POSTGRES_USER` (default `copi`), `POSTGRES_PASSWORD` (required),
  `POSTGRES_DB` (default `copi`), `SECRET_KEY` (required), `ENVIRONMENT` (default `production`),
  `DOMAIN` (required by nginx), `AWS_REGION` (default `us-east-2`, awslogs only) (`prod.yml:6-8,34-38,161,23`).
  `COMPOSE_FILE` in the operator's shell (`CLAUDE.md:44`); MEMORY `org1-prod-access-and-local-copy.md:23`
  says local dev sets `COMPOSE_FILE=docker-compose.yml` in `.env` — verify what prod's `.env` says
  before relying on `-f` flags vs `COMPOSE_FILE` precedence.
- **`.env.example` names** (`.env.example:2-51`): `ORCID_CLIENT_ID`, `ORCID_CLIENT_SECRET`,
  `ORCID_REDIRECT_URI`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, `NCBI_API_KEY`, `ENVIRONMENT`,
  `SECRET_KEY`, `BASE_URL`, `ALLOW_HTTP_SESSIONS`, `SLACK_BOT_TOKEN_<AGENT>` / `SLACK_APP_TOKEN_<AGENT>`
  pairs. (Note: `.env.example` lacks `POSTGRES_PASSWORD` and `DOMAIN`, which `prod.yml` requires.)
- **`src/config.py` Settings fields (name = default)** — env var is the upper-cased name:
  `environment="development"` (`:103`); `orcid_client_id=""`, `orcid_client_secret=""`,
  `orcid_redirect_uri="http://localhost:8000/auth/callback"` (`:106-108`);
  `database_url="postgresql+asyncpg://copi:copi@localhost:5432/copi"` (`:111`);
  `anthropic_api_key=""` (`:114`); `ncbi_api_key=""`, `ncbi_contact_email=""` (`:117-120`);
  `secret_key=INSECURE_SECRET_KEY` (`:123`; guard raises unless `environment` ∈ dev set, `:428-450`);
  `base_url="http://localhost:8000"` (`:124`); `allow_http_sessions=False` (`:128`);
  `slack_config_token=""`, `slack_config_refresh_token=""` (`:134-135`; rotated pair persisted in
  `app_settings` KV, `:130-133`, `CLAUDE.md:152-153`); `slack_enabled: bool|None=None` (None =
  auto-detect from tokens; `false` forces DB-only) (`:137-141`); `aws_region="us-east-2"`,
  `ses_sender_email="noreply@copi.science"`, `ses_reply_domain="reply.copi.science"`,
  `ses_inbound_s3_bucket="copi-inbound-email"`, `ses_inbound_s3_prefix="inbound/"` (`:144-148`);
  `outbound_email_allowlist=""` (empty = everyone) (`:150`); `enable_inbound_email=False` (`:152`);
  `audit_recipients="asu@…,malanjary@…,ahuebschen@…"` (`:155`); `notification_check_interval=300`,
  `inbound_poll_interval=60` (`:158-159`); ~125 `slack_bot_token_<agent>=""` incl.
  `slack_bot_token_grantbot` (`:162-290`; DB column `AgentRegistry.slack_bot_token` is authoritative,
  `.env` is fallback, `CLAUDE.md:169-170`); `posthog_api_key=""` (`:293`);
  `llm_profile_model="claude-opus-5"`, `llm_agent_model="claude-sonnet-5"`,
  `llm_agent_model_opus="claude-opus-5"`, `llm_agent_model_sonnet="claude-sonnet-5"` (`:296,317-319`);
  `worker_poll_interval=5` (`:322`); `active_thread_threshold=3`, `unreviewed_proposal_block_count=2`,
  `max_thread_messages=12`, `interesting_posts_cap=20`, `turn_delay_seconds=0.0`, `daily_post_cap=5`,
  `max_abstracts_other_per_thread=10`, `max_full_text_per_thread=2` (`:325-335`);
  `cohort_isolation_enabled=False`, `cohort_default_policy="open"` (`:342,353`);
  `max_consecutive_reactive_turns=3` (`:360`); `llm_rate_window_seconds=600`,
  `llm_calls_per_load_per_window=8` (`:382-383`); `enable_private_refinement=True` (`:391`).
- `ALEMBIC_LOCK_TIMEOUT_MS` (default 10000) read by `alembic/env.py:66`; `MIGRATE_BACKUP_DIR`,
  `MIGRATE_SERVICE`, `MIGRATE_PG_SERVICE`, `MIGRATE_SNAPSHOT`, `DATABASE_URL` read by
  `run_migration.sh:57-61,211`. `PYTHONPATH=/app` must be passed for any `scripts/*.py` in-container
  (`production-migration.md:557-558`).
- `SLACK_CONFIG_TOKEN`/`SLACK_CONFIG_REFRESH_TOKEN` + public `base_url` needed for admin-UI
  provisioning (`CLAUDE.md:152-153`). `ENABLE_INBOUND_EMAIL` unset on prod as of 2026-08-11
  (`docs/inbound-email.md:41-42`).

---

## 10. Documented gotchas

1. **2026-08-06 restart-policy incident**: stack had been recreated from the dev compose file
   (`restart: no`); host freeze rebooted the box; app/worker/postgres/grantbot stayed dead; nginx
   crash-looped on `host not found in upstream "app:8000"` (`CLAUDE.md:29-34`;
   `docker-compose.override.yml:6-9` — the override had been added 2026-05-26 but never committed and
   was deleted from the tree).
2. **awslogs / AccessDeniedException** without the override file (`CLAUDE.md:36-39`; `override.yml:1-4`).
3. **`--remove-orphans` deletes prod nginx/certbot** (`CLAUDE.md:106`).
4. **Prod bakes agent code**: `build agent` required, restart alone runs stale code (`CLAUDE.md:98-100,108-111`).
5. **Bind mounts** `./profiles`, `./prompts`, `./data` shadow image copies for app/agent/grantbot;
   worker mounts only `./profiles` (`prod.yml:39-41,71-72,95-98,124-127`). Prompt edits are live
   without rebuild for mounted services but need a process restart to reload
   (`production-migration.md:467-468`).
6. **Slack rate limiting on resume**: ~10 min of `Rate limited, retrying in 10s (attempt 1/3)`
   before Turn 1 (`CLAUDE.md:73-77`).
7. **SIGKILL loses the in-flight turn** — use `docker stop -t 30 agent-run` (`CLAUDE.md:88-93`;
   `production-migration.md:193-198`); prod agent has no `stop_grace_period` (§1).
8. **Bare `docker compose` in the migration tooling and runbook** (§2/§3): `run_migration.sh`
   and `production-migration.md:442,460` rely on `COMPOSE_FILE` being exported; the org1-parity spec
   calls it "not optional" (`org1-parity-design.md:418-420`).
9. **run_migration.sh Step 1 requires the running `app` container to carry the new source**
   (`run_migration.sh:109-128`) — the image must be rebuilt from the deploy commit before migrating,
   even though the runbook's rule is "migrate before deploying code" (`production-migration.md:24-26`).
10. **DSN default trap**: `alembic.ini:5` localhost default; only `DATABASE_URL` overrides
    (`env.py:21-23`); `postgres` hostname resolved to a public IP from the dev host
    (`production-migration.md:30-40`). Postgres is not published to the host (`:34-35`).
11. **Alembic output is not evidence** — read `alembic_version` back (`production-migration.md:41-44`;
    `env.py:69-77`; `run_migration.sh:268-293`).
12. **`PYTHONPATH=/app`** for every in-container script or `import src` hits stale site-packages
    (`run_migration.sh:99-105`; `production-migration.md:341-345`).
13. **Lock timeout** 10 s default; idle-in-transaction sessions block the migration and everything
    behind it; stop writers and re-run (`production-migration.md:176-206`).
14. **`docker system prune` / `docker volume prune` on the host destroy unreferenced production
    volumes** (`copi_pgdata`, `copi-prod_pgdata`, `copi-python_grantbot_data`,
    `collab-platform_mongodb_data`) (`...design.md:124-127`; `scripts/ci.sh:165-171`;
    `production-migration.md:137-140`). MEMORY `org1-prod-state-2026-08-14.md:19`: prune also deletes
    stopped `agent-run` containers and their logs — save logs first.
15. **`docker restart` re-runs the OLD environment**; `.env` changes need `up -d` (`inbound-email.md:88-90`).
16. **07:00 UTC daily Claude cron** on the host (`...design.md:470-473`); backup timer at 01:00
    America/Los_Angeles (`copi-backup.timer:5`) — avoid deploy windows overlapping either.
17. **Host RAM is tight**: 3.7 GB, ~1.2 GB swapped, prior OOM kills (`...design.md:624-626`).
18. **Legacy rows after 0019 have `content=''`, `posted_at=0`**; step 8 `backfill_slack_ts.py`
    (report then `--apply`) needs valid bot tokens; exit 2 = UNVERIFIED, not done
    (`production-migration.md:397-437`).
19. **`copi-backup run` silently exits 0 if the flock is held** (`copi_backup.py:1141-1144`) — a manual
    run overlapping the timer does nothing; check `status.json`/journal.
20. **grantbot catch-up run** fires immediately if the container starts after 08:00 UTC on a day it
    has not run (`grantbot.py:758-759,769`).
21. **`--budget N`** deprecated; use `--budget 0` (`src/agent/main.py:254-261`).
22. **`.env.example` is incomplete for prod** — lacks `POSTGRES_PASSWORD`, `DOMAIN` that
    `prod.yml` requires (`prod.yml:7,161`).
23. **`README.md` is stale/dev-only** (`PILOT_LABS` at `README.md:99` vs `CLAUDE.md:121-122`).
24. `SECRET_KEY` guard: with `ENVIRONMENT` defaulting to `production` in prod compose (`prod.yml:38`),
    a missing/default `SECRET_KEY` makes the app refuse to start (`src/config.py:428-450`).
25. `run_migration.sh` hardcodes `pg_dump -U copi` (`:182`) and the runbook hardcodes `-U copi -d copi`
    throughout; `MIGRATE_PG_SERVICE`/`MIGRATE_SERVICE` cover service names only (`:59-60`).
26. `pre-push` hook is the CI gate; MEMORY `org1-prod-state-2026-08-14.md:17`: the hook's throwaway
    Postgres defaults to port 55432 (`scripts/ci.sh:60`) and can collide with a long-lived container —
    `MIGCHECK_PORT=<other> git push`.
