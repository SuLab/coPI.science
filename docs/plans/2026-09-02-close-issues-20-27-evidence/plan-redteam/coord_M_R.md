# Red-team audit — coordination sections + Part M + Part R + cross-part consistency

Scope audited: MASTER_PLAN.md lines 1-69 (header / Global Constraints / Part 0 / Execution order /
Cross-part reconciliation), 19342-19566 (Part M), 19567-19780 (Part R, Opening the PR, Decisions, Coverage).
Everything below was verified against the real tree at `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c
(read-only), the deploy dossier, and the other parts' task text.

---

## 1. Verdict table

| Unit | Verdict | One line |
|---|---|---|
| Header / Global Constraints (L1-24) | needs-fix | Gate figures verified correct (254 findings / ceiling 260 / COV_MIN 60); but "every commit must keep ci.sh green" is impossible for tasks 22.2→20.10 (see MAJOR-1), and no rule tells the executor that in-container pytest must use the **dev** compose file once 27.1 removes `tests/` from the image. |
| Part 0 (0.1, 0.2) | OK | `MIGCHECK_PORT=55433` and the 18ba52c/254/2030/69.27 figures all check out. |
| Execution order (L44-53) | needs-fix | Dependencies are respected, but phase 3 leaves the suite red for 4 commits and the pre-push hook makes pushing impossible mid-phase; `27.3 depends on M.1 (head pin)` is spurious (27.3 uses `alembic upgrade head`, no pin). |
| Cross-part reconciliation (L55-67) | needs-fix | Item 1 ("M.1 is the ONLY task that edits preflight.py / test_migration_checks.py") **directly contradicts** 22.2's and 25.3's own Files lists. Five cross-part file/function overlaps are unlisted (orcid.py, invite.py, models/agent_activity.py `ThreadDecision`, main.py, admin.py/profile.py). Item 6 states the CLAUDE.md order backwards. |
| Task M.1 | **blocker** | 3 blockers (B4 confdeltype/bytes, B5 EXPECTED_INDEXES contract, B6 drift-guard tuple) + incomplete Files list (misses `:223`, `:231`, `EXPECTED_COLUMNS`, `POST_0019_STARTS`) + ruff-failing test code + no fix for `check_sizing`, which at rev 0024 reports a lock estimate for a migration that already ran. |
| Task M.2 | **blocker** | B2 (ephemeral-container snapshot loss makes postflight FAIL after a good migration). Flag/`compose_py`/`-e`-ordering design is otherwise correct and `--no-deps` genuinely does defeat 27.3's `depends_on: migrate`. Test has 4 ruff violations and does not actually verify the `ps` skip. |
| Task M.3 | needs-fix | Doc scope anchor `:7-8` is right, but the doc's **title** (line 1, "migration to alembic 0024") and its "13 checks" count (`:370`, `:569`, `:575`) also drift; step 1's sentence is garbled. |
| R.0 Preconditions | needs-fix | Misses D10 (GrantBot token — 23.4 silently stops funding posts without it) and D4; window logic is correct. |
| R.1 Read state | needs-fix | `{{.State.Health.Status}}` in R.9's loop errors on the 3 services with no healthcheck; no measurement of `publications`/`users` size, which is the actual lock risk; `ROLLBACK_SHA` is prose, never assigned. |
| R.2 Backup | needs-fix | `ls -la /var/backups/copi/copi-python/` needs `sudo` (dir is 0700 root, `install.sh:26`); `PRE_DEPLOY_DUMP` never assigned. `copi-backup run --no-prune` + flock caveat + status.json path all verified correct. |
| R.3 Build | needs-fix | Missing `--no-cache` (required by 27.1's and 27.7's own Deploy notes and by R.3's own "hundreds of MB smaller" expectation) and missing the new `migrate` service from the build list. |
| R.4 Stop writers | **blocker** | B3 — "Leave `app` and `worker` running" is contradicted by preflight's own remediation text and by the real lock footprint of the 0025→0028 chain. |
| R.5 chown | needs-fix | 27.8's Deploy note chowns `profiles`, `data` **and `prompts`**; R.5 omits `prompts`. |
| R.6 Migrate | **blocker** | B2 (snapshot) + B3 (locks). Also: password in the DSN is not percent-encoded; the `LockNotAvailableError` remediation omits app/worker; no remedy path for a preflight check-7 BLOCK. |
| R.7 Recreate | **blocker** | B1 — `migrate` is missing from `docker-compose.override.yml`, so `up -d app worker grantbot` fails the dependency and the stack stays down. Plus: `curl http://127.0.0.1:8000` can never work (no published port), the expected health body `{"status":"ok","db":"ok"}` is wrong (27.2 returns `{"status":"ok"}`), and `docker compose ps` won't show the exited `migrate` without `-a`. |
| R.8 nginx | OK (minor) | `exec -T nginx nginx -t` / `nginx -s reload` are valid for `nginx:1.27-alpine` (the container's own command runs `nginx -g "daemon off;"`, so the master owns the pidfile). `up -d nginx` will also (re)run `migrate` via app's new `depends_on` — worth a note. |
| R.9 Agent + checks | needs-fix | Two greps can never match: `=== Turn 1 ===` (real format is `=== Turn %d: %s ===`) and `Roster sync` (real prefix is `[roster]`, and it only logs on change). `docker inspect ... Health.Status` errors for worker/grantbot/certbot. |
| R.10 Rollback | needs-fix | `pg_restore -Fc` over `docker exec -i` stdin is correct (design-doc precedent) and the "old code on new schema" analysis is right for 0025/0027/0028; 0026's direction is safe too but the note is silent on it. `$PRE_DEPLOY_DUMP` / `$ROLLBACK_SHA` unassigned; no warning that `up -d` before the `git checkout` would re-apply 0025-0028 via the new `migrate` service. |
| R.11 Blackbird | OK | Matches the dossier and 27.1's Deploy note. |
| Opening the PR | needs-fix | `Closes #20` is claimed while D9's fix (Task 20.9b) does not exist anywhere in the plan. |
| Decisions table | needs-fix | D14's numbers contradict the implementation (see MAJOR-9); D22's consumer doesn't cover the named file; 8 open decisions raised inside parts are absent. |
| Coverage section | needs-fix | The closure claim ("items no part owns are exactly D4, D9, D21, D23") is false. |

Counts: **6 BLOCKER · 18 MAJOR · 18 MINOR**

---

## 2. BLOCKERS

### B1 — The `migrate` service will not start on prod, and it now gates the whole stack (R.7, 27.3)
`docker-compose.override.yml` (verified, 34 lines) enumerates services **explicitly**: `postgres, app,
worker, agent, grantbot, nginx, certbot`. Task 27.3's new `migrate` service sets
`logging: driver: awslogs` and is **not added to the override**. Per `CLAUDE.md:36-39` the EC2 role
lacks `logs:CreateLogStream`, so an awslogs container "dies at start with `AccessDeniedException`".
Because 27.3 also adds `depends_on: migrate: condition: service_completed_successfully` to `app`,
`worker` and `grantbot`, R.7's `docker compose $C up -d app worker grantbot` gets
`dependency failed to start: container copi-python-migrate-1 exited (1)` and **none of the three
services start**. R.8's `up -d nginx` then fails the same way (nginx → app → migrate). Prod is down
with no rollback step in R for this failure mode.

**Corrected text — append to Task 27.3 Step 3:**
```yaml
# docker-compose.override.yml — the awslogs driver in docker-compose.prod.yml is
# unusable on this instance role (CLAUDE.md:36-39); every service in prod.yml MUST
# have a json-file entry here or it dies at start with AccessDeniedException.
  migrate:
    logging:
      driver: json-file
```
**and add to Task 27.3 Step 1 (`tests/unit/test_deploy_compose.py`) a general guard:**
```python
def test_every_prod_service_has_a_json_file_logging_override():
    prod = set(_prod_compose()["services"])
    override = set(yaml.safe_load((REPO_ROOT / "docker-compose.override.yml").read_text())["services"])
    assert prod <= override, f"missing json-file override for: {sorted(prod - override)}"
```
**and insert into Part R before R.7's `up`:**
```bash
docker compose $C config | grep -A2 -E '^  (migrate|app):' | grep -c 'awslogs' # expect 0
docker compose $C run --rm --no-deps -T migrate python -m alembic current       # dry proof the image + DSN work
```
**and add to R.7 after the `up`:**
```bash
docker compose $C ps -a migrate                       # must be Exited (0)
docker compose $C logs migrate | tail -20             # must NOT contain AccessDeniedException
```
If `migrate` exited non-zero, `docker compose $C up -d --no-deps app worker grantbot` is the
escape hatch (it bypasses the gate); fix the override file and rebuild before the next deploy.

### B2 — `--via-run` destroys the preflight row-count snapshot, so postflight FAILs after a *successful* migration (M.2, R.6)
`run_migration.sh` passes `--snapshot "$SNAP"` (default `backups/preflight_snapshot.json`) to preflight
(`:211-215`) and the *same path* to postflight (`:301`). Under `exec` both run inside the one
long-lived `app` container, so the file persists. Under `docker compose run --rm` each step gets a
**fresh, immediately-deleted** container. Verified in `postflight.py:557-561`:
```python
    p = Path(snapshot_path)
    if not p.is_file():
        return (title, FAIL, f"snapshot {snapshot_path} does not exist.", ...)
```
`FAIL` → `run_postflight` exit 1 → `run_migration.sh` Step 7 exits `EX_BLOCKED` printing *"Do NOT deploy
application code"* — **after the chain has already committed**. R.6 then instructs: "If postflight prints
any FAIL: do not deploy code … go to R.10", i.e. rename the DB and restore a dump over a perfectly good
migration. This also silently disables the only row-loss check in the whole runbook.

(Non-issue, checked: `write_snapshot` does `p.parent.mkdir(parents=True, exist_ok=True)`
(`preflight.py:1810-1812`) and 27.8 `chown -R 10001:10001 /app`, so the missing `/app/backups`
directory after 27.1 excludes `backups/` from the image is **not** a problem.)

**Corrected text — Task M.2 Step 3, replace the `compose_py` definition:**
```bash
# The snapshot is a handoff FILE between two separate script steps. With --via-run each
# step is a `--rm` container, so it must live on a bind mount or postflight cannot see it.
SNAP_HOST_DIR="$(cd "$(dirname "$SNAP")" && pwd)"
compose_py() {   # replaces the inline `docker compose exec ...` in Step 1, run_py() and Step 5
  if [[ "$VIA_RUN" == "1" ]]; then
    docker compose run --rm --no-deps -T \
      -v "$SNAP_HOST_DIR:/migrate-state" \
      -e MIGRATE_STATE_DIR=/migrate-state \
      -e PYTHONPATH=/app -e DATABASE_URL="$DSN" "$@"
  else
    docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL="$DSN" "$@"
  fi
}
```
and, immediately after `SNAP` is resolved at `:211`, add:
```bash
mkdir -p "$(dirname "$SNAP")"
SNAP_HOST_DIR="$(cd "$(dirname "$SNAP")" && pwd)"
# Inside a --via-run container the snapshot must be addressed through the bind mount,
# not through the (ephemeral) image path.
SNAP_IN_CONTAINER="$SNAP"
[[ "$VIA_RUN" == "1" ]] && SNAP_IN_CONTAINER="/migrate-state/$(basename "$SNAP")"
```
then use `"$SNAP_IN_CONTAINER"` in the two `--snapshot` arguments (`:214`, `:301`).
Add to M.2 Step 1's test:
```python
    assert " -v " in argv and "/migrate-state" in argv, "snapshot must be on a bind mount under --via-run"
```
Add to R.6, after the `--apply` line:
```bash
ls -l backups/preflight_snapshot.json    # must exist on the HOST and be newer than the rehearsal
```

### B3 — R.4's "leave `app` and `worker` running" is wrong; the tooling itself says to stop them (R.4, R.6)
Two independent confirmations:

1. **preflight check 7 will BLOCK.** `blocking_sessions_status` (`preflight.py:474-517`) returns
   `BLOCK` on **any** idle-in-transaction session *of any age*, and `BLOCK` on any transaction older
   than 5 s. Its own remediation list (`preflight.py:1522-1531`) is literally:
   ```
   docker stop -t 30 agent-run
   docker compose stop app worker grantbot
   ```
   R.6 gives no remedy for a check-7 BLOCK ("read the check name, fix, re-run"), and R.6's
   `LockNotAvailableError` advice names only `agent-run` and `grantbot`.
2. **The lock footprint is much wider than "every new object is tolerated".** `alembic/env.py`
   runs the whole chain in **one transaction** (dossier §3; no `transaction_per_migration`), so every
   lock below is held from the moment it is taken until the *end of the chain*:
   * `0025` `ALTER TABLE publications ADD CONSTRAINT … UNIQUE (user_id, pmid)` → **ACCESS EXCLUSIVE on
     `publications`** for the whole index build (blocks reads *and* writes; the worker's profile
     pipeline inserts here, the dashboards read here).
   * `0026` `drop_constraint` + `create_foreign_key(..., ondelete="CASCADE")` → **ACCESS EXCLUSIVE on
     `private_channel_members`** and **SHARE ROW EXCLUSIVE on `users`** (blocks every INSERT/UPDATE/DELETE
     on `users` — including the `last_login_at` write on every login).
   * `0027` 20 non-concurrent `CREATE INDEX` → **SHARE** on 13 tables (`agents`, `thread_decisions`,
     `proposal_reviews`, `email_notifications`, `private_channel_members`, `cohorts`,
     `cohort_memberships`, `cohort_audit_events`, `access_allowlist`, `agent_delegates`,
     `delegate_invitations`, `profile_revisions`, `slack_app_provisions`) — blocks all writes.
   * `0028` `ADD COLUMN reopened_at` nullable → ACCESS EXCLUSIVE on `thread_decisions` (fast, but adds
     to the same held set).
   With `ALEMBIC_LOCK_TIMEOUT_MS=10000` the realistic outcome with app+worker live is a repeated
   `LockNotAvailableError` (a pending ACCESS EXCLUSIVE also queues ahead of new readers — see the
   measured note at `preflight.py:479-484`), and if it *does* acquire, every app request touching those
   tables hangs until commit, exhausting the uvicorn pool.

**Corrected R.4:**
```bash
mkdir -p logs
docker logs agent-run > logs/run_$(date +%s).log 2>&1 || true
ls -t logs/run_*.log | tail -n +11 | xargs -r rm -f
docker stop -t 30 agent-run || true ; docker rm agent-run || true   # SIGTERM+30s flushes the in-flight turn
docker compose $C stop grantbot worker                              # worker inserts into publications
# app LAST, so the window with no web service is as short as possible. It must go too:
#  - preflight check 7 BLOCKs on any idle-in-transaction session (preflight.py:474-494) and its own
#    remediation says `docker compose stop app worker grantbot`;
#  - the chain is ONE transaction, so 0025's ACCESS EXCLUSIVE on publications, 0026's SHARE ROW
#    EXCLUSIVE on users and 0027's SHARE on 13 tables are all held until the last statement commits.
docker compose $C stop app
# nginx now 502s (static `upstream app { server app:8000; }`, nginx.conf:38-40) and its healthcheck
# goes unhealthy. That is EXPECTED and acceptable for the ~5-15 min window; do not restart nginx here.
docker compose $C exec -T postgres psql -U copi -d copi -c \
  "select pid,state,now()-xact_start age,left(query,60) from pg_stat_activity
    where datname=current_database() and pid<>pg_backend_pid() and backend_type='client backend'"
# expect: no rows (or only your own psql). Anything else must be understood before R.6.
```
Add to R.6's `LockNotAvailableError` paragraph: *"…make sure `agent-run` is gone and **app, worker and
grantbot are stopped** (R.4), then re-run `--apply`. If it still times out, re-run with
`ALEMBIC_LOCK_TIMEOUT_MS=60000` — the knob bounds lock *wait*, not statement duration
(`alembic/env.py:64-65`)."*
Add to R.1: `docker compose $C exec -T postgres psql -U copi -d copi -c "select relname, pg_size_pretty(pg_total_relation_size(c.oid)) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and relkind='r' order by pg_total_relation_size(c.oid) desc limit 10"`
— `publications` and `users` are the two that decide how long R.6's window is.

### B4 — M.1's FK-action check compares a Postgres `"char"` to a Python `str`; asyncpg returns `bytes`
M.1's new integration test:
```python
q = text("""select conname, confdeltype from pg_constraint …""")
…
        assert rows[r.conname] == action          # 'c' = CASCADE, 'n' = SET NULL
```
`pg_constraint.confdeltype` is Postgres' internal `"char"` type — exactly the type
`preflight.py:914-919` has a load-bearing comment and a dedicated regression test
(`tests/unit/test_migration_checks.py::test_existing_object_names_casts_relkind_to_text`) about:
*"asyncpg decodes it to BYTES (b'r', b'i'), so `row["k"] == "r"` is silently always False"*.
So `b'c' == 'c'` is False and the test fails against a **correct** schema; and if `check_fk_actions`
(which M.1 describes but does not write) repeats the pattern, postflight FAILs every run and blocks
every deploy.

**Corrected text — drop `EXPECTED_FK_ACTIONS` and `check_fk_actions` entirely.** `CONSTRAINTS_SQL`
(`postflight.py:262-269`) has **no `contype` filter** and already returns `pg_get_constraintdef`, so
the FK direction is checkable with two rows in the dict that already exists and the check function
that already runs:
```python
#: constraint name -> (table, pg_get_constraintdef)
EXPECTED_CONSTRAINTS: dict[str, tuple[str, str]] = {
    ...existing two entries...,
    # 0025
    "uq_publications_user_pmid": ("publications", "UNIQUE (user_id, pmid)"),
    # 0026 flips this one from SET NULL to CASCADE; the added_by_user_id row is the
    # contrast case that must NOT change (see 0026's docstring).
    "private_channel_members_user_id_fkey": (
        "private_channel_members",
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
    ),
    "private_channel_members_added_by_user_id_fkey": (
        "private_channel_members",
        "FOREIGN KEY (added_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
}
```
(Constraint names verified: `alembic/versions/0011_…py:63-75` creates both FKs **unnamed** inside
`op.create_table`, so Postgres assigned `<table>_<column>_fkey` — matching 25.1's `_FK` and M.1's names.)
Replace the second integration test with:
```python
async def test_postflight_expected_constraint_defs_at_head(engine):
    q = text("select con.conname n, rel.relname t, pg_get_constraintdef(con.oid) d "
             "from pg_constraint con join pg_class rel on rel.oid=con.conrelid "
             "join pg_namespace ns on ns.oid=con.connamespace where ns.nspname='public'")
    async with engine.connect() as conn:
        live = {r.n: (r.t, r.d) for r in (await conn.execute(q)).all()}
    for name, (table, expect) in post.EXPECTED_CONSTRAINTS.items():
        assert name in live, name
        assert live[name][0] == table, (name, live[name])
        assert expect in live[name][1], (name, live[name][1])
```
If a separate check *is* wanted anyway, the cast is mandatory: `confdeltype::text AS confdeltype`.

### B5 — M.1 as written makes an existing unit test fail (`uq_publications_user_pmid` must also be in `EXPECTED_INDEXES`)
`tests/unit/test_migration_checks.py:1072-1074`:
```python
def test_postflight_expects_an_index_for_every_index_the_chain_creates():
    planned = {o.name for o in pf.PLANNED_OBJECTS if o.kind in {"index", "constraint"}}
    assert planned <= set(po.EXPECTED_INDEXES)
```
M.1 adds `PlannedObject("0025", "constraint", "uq_publications_user_pmid", "publications")` but puts
it only in `EXPECTED_CONSTRAINTS`. The set inclusion then fails. (This is why
`uq_agent_messages_run_ts` and `uq_cohort_membership_cohort_agent` appear in **both** dicts today —
`postflight.py:143-162` and `:164-173`.)

**Corrected text — M.1's `EXPECTED_INDEXES` addition must be 21 entries, not 20:**
```python
EXPECTED_INDEXES: dict[str, str] = {
    ...existing 13 entries...,
    # 0025 — a UNIQUE constraint also materialises a unique index of the same name.
    "uq_publications_user_pmid": "USING btree (user_id, pmid)",
    # 0027 (copy the column lists from alembic/versions/0027_fk_and_badge_indexes.py::_INDEXES)
    "ix_access_allowlist_added_by_user_id": "USING btree (added_by_user_id)",
    "ix_agent_delegates_user_id": "USING btree (user_id)",
    "ix_agent_delegates_invitation_id": "USING btree (invitation_id)",
    "ix_agents_approved_by": "USING btree (approved_by)",
    "ix_cohort_audit_events_actor_id": "USING btree (actor_id)",
    "ix_cohort_memberships_added_by": "USING btree (added_by)",
    "ix_cohorts_created_by": "USING btree (created_by)",
    "ix_delegate_invitations_invited_by_user_id": "USING btree (invited_by_user_id)",
    "ix_delegate_invitations_accepted_by_user_id": "USING btree (accepted_by_user_id)",
    "ix_email_notifications_thread_decision_id": "USING btree (thread_decision_id)",
    "ix_email_notifications_agent_registry_id": "USING btree (agent_registry_id)",
    "ix_private_channel_members_user_id": "USING btree (user_id)",
    "ix_private_channel_members_added_by_user_id": "USING btree (added_by_user_id)",
    "ix_profile_revisions_changed_by_user_id": "USING btree (changed_by_user_id)",
    "ix_proposal_reviews_user_id": "USING btree (user_id)",
    "ix_proposal_reviews_delegate_user_id": "USING btree (delegate_user_id)",
    "ix_proposal_reviews_reviewed_by_user_id": "USING btree (reviewed_by_user_id)",
    "ix_slack_app_provisions_agent_registry_id": "USING btree (agent_registry_id)",
    "ix_thread_decisions_agent_a_outcome": "USING btree (agent_a, outcome)",
    "ix_thread_decisions_agent_b_outcome": "USING btree (agent_b, outcome)",
}
```
Also add to M.1's Files list: `scripts/migrate/postflight.py — EXPECTED_COLUMNS (:97-131)` (the
reconciliation requires the `reopened_at` row there, and
`test_postflight_expects_a_column_for_every_column_the_chain_creates` (:1082-1085) enforces it), and
correct the two drifted anchors: `EXPECTED_INDEXES` is at **:143-162** (not :141-156) and
`EXPECTED_CONSTRAINTS` at **:164-173** (not :159-168).
`MUST_BE_NON_NULL` needs no edit — it is derived (`postflight.py:182-184`) and `reopened_at` is nullable.

### B6 — "Bump the '0024' literals at :838" silently disables the drift guard for 0025-0027
Line 838 is not a pin, it is the drift guard's iteration set:
```python
    for revision in ("0019", "0020", "0021", "0022", "0023", "0024"):
```
inside `test_planned_objects_matches_what_the_migration_files_actually_create` (`:832-865`) — the one
mechanism that keeps `PLANNED_OBJECTS` honest against the migration files. Bumping the *literal*
`"0024"` → `"0028"` yields `("0019","0020","0021","0022","0023","0028")`: 0024 drops out and
0025/0026/0027 are never checked at all.

**Corrected text (M.1 Step 3):**
```python
    for revision in ("0019", "0020", "0021", "0022", "0023", "0024",
                     "0025", "0026", "0027", "0028"):
```
**And a caveat the plan must state:** 0027's migration creates its indexes in a **loop**
(`for name, table, columns in _INDEXES: op.create_index(name, table, list(columns))`, Task 25.3
Step 3), so the guard's regex `create_index\(\s*\n?\s*"([^"]+)"` finds **zero** names in 0027 and the
guard is vacuous there. Add to M.1 Step 1:
```python
def test_0027_index_list_matches_planned_objects_exactly():
    """0027 builds its indexes in a loop, so the create_index regex drift guard sees nothing.
    Compare the migration's own _INDEXES table against PLANNED_OBJECTS instead."""
    import importlib.util, pathlib
    p = next(pathlib.Path("alembic/versions").glob("0027_*.py"))
    spec = importlib.util.spec_from_file_location("mig0027", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from_file = {(n, t) for n, t, _c in mod._INDEXES}
    from_pf = {(o.name, o.table) for o in pf.PLANNED_OBJECTS if o.revision == "0027"}
    assert from_file == from_pf
```
(Verified by hand: M.1's 20 `PlannedObject("0027", …)` entries and 25.3's `_INDEXES` are an **exact
match**, names and tables. None of the 21 new object names exists anywhere in `alembic/` or `src/`
today, so preflight check 5 will not spuriously BLOCK.)

---

## 3. MAJOR findings

### MAJOR-1 — Phase 3 leaves `./scripts/ci.sh` red for four commits, which the Global Constraints forbid and the pre-push hook enforces
Global Constraints: *"every commit must keep `./scripts/ci.sh` green"*. But the moment 22.2 lands
(alembic head 0025, `REVISION_ORDER` extended, `DEFAULT_TARGET` still `"0024"`) these existing tests
fail, and stay failing until M.1:
* `tests/integration/test_harness_smoke.py:15` — `assert v == "0024"`
* `test_revision_order_covers_the_supported_range` (`:952-954`) — `REVISION_ORDER[-1] == DEFAULT_TARGET`
* `test_alembic_scripts_agrees_with_the_real_tree` (`:946-948`) — `data["heads"] == [DEFAULT_TARGET]`
* `test_planned_objects_between_0018_and_the_target_is_everything` (`:789-790`)

Because `pre-push` runs `ci.sh` (CLAUDE.md:5-8, and there is no server-side CI), the executor
**cannot `git push`** anywhere between 22.2 and M.1.

**Corrected text — add to the Execution order table, phase 3 row:**
> **Phase 3 is a single atomic push unit.** From 22.2's commit until M.1's commit the tooling head
> pins disagree with the tree and `./scripts/ci.sh` is RED by design (4 known failures:
> `test_harness_smoke::test_container_is_migrated`, `test_revision_order_covers_the_supported_range`,
> `test_alembic_scripts_agrees_with_the_real_tree`,
> `test_planned_objects_between_0018_and_the_target_is_everything`). Commit each task, but do **not**
> `git push` (the pre-push hook runs `ci.sh`) and do not treat those four failures as new breakage
> until after M.1. Run the GATE only once, after M.3.

Also amend Global Constraints bullet 1 to *"every commit must keep `./scripts/ci.sh` green **except
inside phase 3, where the head pins are deliberately bumped once at the end (M.1)**"*.

### MAJOR-2 — Reconciliation item 1 contradicts Tasks 22.2 and 25.3
Item 1: *"M.1 … is the **ONLY** task that edits `scripts/migrate/preflight.py`, `scripts/migrate/postflight.py`,
`scripts/migrate/run_migration.sh:56`, `tests/integration/test_harness_smoke.py:15` and the head pins in
`tests/unit/test_migration_checks.py`."*
But **Task 22.2's Files list** says: `Modify: scripts/migrate/preflight.py — PLANNED_OBJECTS, REVISION_ORDER`
and `Modify: tests/unit/test_migration_checks.py — the drift-guard revision loop (:838)`, and its
Step 3 shows the exact diff for both. **Task 25.3's Files list** names `preflight.py — DEFAULT_TARGET (:74),
PLANNED_OBJECTS (:168-204), REVISION_ORDER (:206)`, `run_migration.sh — TARGET (:56)`,
`test_migration_checks.py — :232, :838, :1129, :1170`, `test_harness_smoke.py`. The executor of 22.2
gets two contradictory instructions.

**Corrected item 1 (replace the "ONLY task" sentence):**
> M.1 is the sole owner of the **head pins** — `preflight.DEFAULT_TARGET`, `preflight.REVISION_ORDER`'s
> final element, `preflight.SUPPORTED_START_REVISIONS`, `preflight.POST_0019_STARTS`,
> `run_migration.sh:3` and `:56`, `tests/integration/test_harness_smoke.py:15`, and the `"0024"`
> assertions at `tests/unit/test_migration_checks.py:223, 231, 232, 1129, 1170` — and the sole owner of
> **all of `scripts/migrate/postflight.py`**. Tasks 22.2 and 25.3 *do* still add their own
> `PLANNED_OBJECTS` entries and extend `REVISION_ORDER` and the drift-guard loop at
> `test_migration_checks.py:838`, because the drift guard is what keeps those entries honest; M.1
> reconciles the final list. **25.3's `DEFAULT_TARGET` / `run_migration.sh:56` /
> `test_harness_smoke.py` / `:232` / `:1129` / `:1170` sub-steps are skipped** (M.1 does them); 22.3 is
> verify-only.

### MAJOR-3 — M.1's Files list misses two "0024" sites that will break, and one whose semantics change
`grep -n 0024 tests/unit/test_migration_checks.py` gives **five** hits: `223, 232, 838, 1129, 1170`.
M.1 lists four (misses `:223`). And `:231` has no literal `0024` but pins the whole tuple:
```python
223:@pytest.mark.parametrize("rev", ["0001", "0017", "0022", "0024", "abcdef"])
224:def test_revision_status_blocks_anywhere_else(rev):
231:    assert pf.SUPPORTED_START_REVISIONS == ("0018", "0019", "0020", "0021", "0023")
```
Adding `"0024"` to `SUPPORTED_START_REVISIONS` makes `revision_status("0024", "0023")` return `PASS`
(`preflight.py:386-387`), so `test_revision_status_blocks_anywhere_else[0024]` **fails**.

**Corrected M.1 Step 3 additions:**
```python
# tests/unit/test_migration_checks.py:223 — 0024 is now a supported start (org1 sits there).
@pytest.mark.parametrize("rev", ["0001", "0017", "0022", "abcdef"])

# :231-232
    assert pf.SUPPORTED_START_REVISIONS == ("0018", "0019", "0020", "0021", "0023", "0024")
    assert pf.DEFAULT_TARGET == "0028"
```
Add both line numbers to M.1's Files list.

### MAJOR-4 — Nothing fixes `check_sizing`, so preflight's lock-window estimate at 0024 is actively misleading (and 25.3's Deploy note is false)
`check_sizing` (`preflight.py:1612-1650`) is entirely about `agent_messages` and **0019's** index build.
It short-circuits only for `rev in POST_0019_STARTS`, and `POST_0019_STARTS = ("0020", "0021")`
(`preflight.py:93`). At `rev="0024"` it therefore falls through to
`sizing_status(agent_messages_rows, …)` and reports a row-scaled worst-case lock window for a
migration that **already ran three revisions ago** — WARNing "schedule a window, do not migrate hot"
above 10 s, which can push the R.6 rehearsal to exit 2 for no reason.

Task 25.3's Deploy note claims *"if any of these tables is large in prod, preflight's check 9
(lock-window estimate) will flag it before `--apply`"* — **false**: check 9 never looks at
`publications`, `users`, or any of the 13 tables 0027 indexes.

**Corrected text — add to M.1's Files list** `scripts/migrate/preflight.py — POST_0019_STARTS (:93)`
and `tests/unit/test_migration_checks.py — test_sizing_does_not_quote_the_0019_index_build_once_0019_has_run (:251-256)`, **and to M.1 Step 3:**
```python
#: Start revisions at which migration 0019 has already run, so the expensive
#: ACCESS EXCLUSIVE index build on agent_messages is behind us.
POST_0019_STARTS = ("0020", "0021", "0023", "0024")
```
and extend `check_sizing`'s `rev in POST_0019_STARTS` PASS branch so it also sizes the objects the
0025-0028 chain actually builds:
```python
        pubs = int(await fetch_one_value(conn, "SELECT count(*) FROM publications")) \
            if await table_exists(conn, "publications") else 0
        floor_ms, hi = lock_window_ms(pubs)   # same calibration, applied to the table 0025 rewrites
        return (
            title,
            sizing_status(pubs, hi)[0],
            f"agent_messages: {rows:,} rows, heap {heap / 1e6:.1f} MB — 0019 is behind you at {rev}. "
            f"What remains for 0024->0028 is: 0025 ADD CONSTRAINT UNIQUE on publications "
            f"({pubs:,} rows) which takes ACCESS EXCLUSIVE for the whole index build; 0026 "
            f"drop/recreate of one FK (ACCESS EXCLUSIVE on private_channel_members, SHARE ROW "
            f"EXCLUSIVE on users); 0027's 20 non-concurrent CREATE INDEXes (SHARE on 13 tables); "
            f"0028 one nullable ADD COLUMN. The chain is ONE transaction, so every one of those "
            f"locks is held until the last statement commits. {sizing_status(pubs, hi)[1]}",
            ["Stop app, worker, grantbot and agent-run before --apply (see the runbook R.4)."],
            {"agent_messages_rows": rows, "publications_rows": pubs},
        )
```
and update `test_sizing_does_not_quote_the_0019_index_build_once_0019_has_run` to the new tuple.
Finally, fix 25.3's Deploy note: replace the check-9 sentence with *"measure `publications` and `users`
with `pg_total_relation_size` in R.1; check 9 does **not** size the 0025-0028 objects unless M.1's
extension lands."*

### MAJOR-5 — R.7's expected health-check output is wrong, and its `curl` can never work
R.7: `curl -fsS http://127.0.0.1:8000/api/health` — prod's `app` has **`expose: 8000` and no
`ports:`** (`docker-compose.prod.yml:30-31`; postgres/app publish nothing, confirmed
`docs/production-migration.md:34-35`). The host has nothing on 8000, so this always fails to the
fallback. And Task 27.2's implementation returns **`{"status": "ok"}`** — no `db` key:
```python
        return {"status": "ok"}
```
R.7's stated expectation `{"status":"ok","db":"ok", ...}` therefore triggers Part R's own STOP rule
("if any command's output contradicts a stated expectation, STOP").

**Corrected R.7:**
```bash
docker compose $C exec -T app python -c \
  'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/api/health").read())'
# Expect exactly b'{"status":"ok"}'. Task 27.2 makes this route PROBE the DB and return 503 when it
# cannot connect; the 200 body is unchanged. Do not expect a "db" key.
curl -fsS http://127.0.0.1/api/health -o /dev/null -w '%{http_code} %{redirect_url}\n'   # via nginx (80 is published)
```

### MAJOR-6 — Two of R.9's greps can never match
* `=== Turn 1 ===` — the real format string is `logger.info("=== Turn %d: %s ===", turn_count + 1, agent.agent_id)`
  (`src/agent/simulation.py:745`), rendering as `=== Turn 1: su ===`. The pattern never fires, so the
  operator watches forever. (CLAUDE.md:74-76 has the same inaccuracy; worth fixing in 26.7.)
* `docker logs agent-run 2>&1 | grep -c 'Roster sync'   # ≥1 after a few minutes` — **the string
  "Roster sync" does not exist in `src/`.** The real lines are `[roster] …`
  (`simulation.py:4574, 4635, 4650, 4662, 4677`) and they fire **only on change**, so a steady roster
  logs nothing. This check is unsatisfiable. (The deploy dossier §7's "Roster sync confirmation:
  INFO line fires on change (src/agent/simulation.py:381)" is itself wrong — line 381 is the *cohort*
  log-signature comment. R inherited the error.)

**Corrected R.9:**
```bash
docker logs -f agent-run 2>&1 | grep --line-buffered -E 'Rate limited|=== Turn [0-9]+:|Traceback|\[roster\]'
```
and in the post-deploy block, replace the roster grep with:
```bash
docker logs agent-run 2>&1 | grep -c 'roster sync failed'    # must be 0
docker logs agent-run 2>&1 | grep -c 'publication-record load failed'   # must be 0
# Positive confirmation of the roster loop is via the UI, not the log (the [roster] INFO lines only
# fire on a change): open /admin/agents and confirm the active roster matches AgentRegistry.
```
(`publication-record load failed` verified present at `simulation.py:4561`; `Rate limited, retrying`
at `slack_client.py:333`.)

### MAJOR-7 — `docker inspect -f '{{.State.Health.Status}}'` errors for the three services with no healthcheck
R.9's post-deploy loop runs it over `app worker postgres grantbot nginx certbot`. Only
`postgres`, `app` and `nginx` define a `healthcheck:` (`docker-compose.prod.yml:11-16, 45-50, 165-170`);
`worker`, `grantbot` and `certbot` do not, so `.State.Health` is nil and the Go template errors out
(`nil pointer evaluating *types.Health.Status`) instead of printing anything.

**Corrected:**
```bash
for s in app worker postgres grantbot nginx certbot; do
  docker inspect copi-python-$s-1 -f \
    "$s {{.State.Status}} {{.HostConfig.RestartPolicy.Name}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}"
done
```

### MAJOR-8 — R.3 omits `--no-cache` and the `migrate` service, contradicting 27.1's and 27.7's Deploy notes
Task 27.1 Deploy note item 3 and Task 27.7's Deploy note both prescribe:
```bash
docker compose $C build --no-cache app worker grantbot
docker compose $C --profile agent build --no-cache agent
```
R.3 uses plain `build`. R.3's own STOP condition ("the new image is hundreds of MB smaller … if it is
not, `.dockerignore` did not take effect — STOP") is the check that depends on it. R.3 also never
builds `migrate`, so it gets built implicitly during R.7 — inside the window, and with no chance to
inspect it first.

**Corrected R.3:**
```bash
docker compose $C build --no-cache app worker grantbot migrate
docker compose $C --profile agent build --no-cache agent
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}} {{.CreatedAt}}' | grep copi-python
docker run --rm --entrypoint sh $(docker compose $C images -q app) -c 'ls /app | sort'   # no backups/, no tests/
```

### MAJOR-9 — Decisions table D14's numbers contradict the implementation
D14 says *"Stale-processing reaper threshold / backoff constants — **30 min / 30 s × 2ⁿ**"*. The plan
actually implements (MASTER_PLAN L5267-5268, L5512-5513):
```python
JOB_RETRY_BACKOFF_BASE_SECONDS = 5.0
JOB_RETRY_BACKOFF_CAP_SECONDS = 300.0
JOB_STALE_PROCESSING_THRESHOLD_SECONDS = 900   # 15 minutes
JOB_REAP_CHECK_INTERVAL_SECONDS = 300
```
and Part 21's own open decision #3 says *"proposed 15 min stale threshold, 5s/300s exponential backoff
base/cap"*. The operator would be approving values that do not exist.

**Corrected D14 row:**
| D14 | Stale-processing reaper threshold / retry backoff constants | **900 s (15 min) stale threshold, 300 s reaper check interval, backoff `5 s × 2^(attempts-1)` capped at 300 s** — validate against real pipeline durations after a week (the onboarding page itself quotes 1-3 min per profile) | 21.3, 21.4 |

### MAJOR-10 — `Closes #20` is claimed but D9's fix (Task 20.9b) does not exist in the plan
D9: *"COR-5 residual on the web path (`post_agent_message` accepts free-form `thread_ts`) — unowned.
Default: **add to Part 20 as Task 20.9b before merge**"*. There is no Task 20.9b anywhere in
MASTER_PLAN.md, yet the PR body says `Closes #20, #21, …`. Part 20's own decision #3 spells out the
consequence: *"a PI can still name another lab's thread_ts on the web form and clear that lab's review
block"*. Shipping `Closes #20` on that basis is wrong, and it is the only security-relevant residual
in the set.

**Corrected text:** either write Task 20.9b (owner: Part 20, function `post_agent_message`, which
reconciliation item 7 currently assigns to "nobody"), or change the PR body's closing line to
`Closes #21, #22, #23, #24, #25, #26, #27` and `Refs #20` with a one-line explanation, and move D9
into the "What NOT" section explicitly.

### MAJOR-11 — Eight open decisions raised inside parts are missing from the Decisions table, and the Coverage claim is therefore false
Missing rows (grepped every `## Open decisions` section and every in-task `Open decision`):
1. **Part 20 #4 / Task 20.12** — COR-10(3): "retry the handler next tick" deliberately not implemented;
   a PI-specific trigger can be lost for one row on a transient failure. No table row.
2. **Part 22 #4 / Task 22.10** — private-profile save on a missing `ResearcherProfile`: create the row
   (default) vs return 400. No table row.
3. **Part 22 #5a** — V1-15f: pre-0023 "legacy" grounded profiles are not protected by the
   `lost_evidence` gate. Explicitly "say if it should be a follow-up". No table row.
4. **Part 22 #5b** — V6-24e: the pipeline's disk export can still land ahead of its DB commit. No row.
5. **Part 24 #2** — true-concurrency testing for V5 needs a `conftest.py` fixture change
   (`join_transaction_mode="create_savepoint"` makes two "concurrent" sessions non-racing). No row.
6. **Part 25 #1 / L15269** — nginx `location /static/` needs `static/` bind-mounted into the nginx
   container (`docker-compose.prod.yml:156-157` mounts only `nginx.conf`). This is a **deploy-topology**
   decision and belongs in Part R's table. No row.
7. **Part 26 #2** — consolidate `simulation.py:5467`'s private JSON extractor onto 26.12's public
   `extract_json` (a Part-20-owned file). No row.
8. **Part 26 #3** — AGENT.md Decisions Log: in-place correction vs appending a superseding entry. No row.
9. **Part 26 #4** — `docs/production-migration.md`'s "Supported starting points 0018-0021" left
   unwidened. This is now **partly resolved by M.1/M.3** (0024 becomes a supported start, and M.3
   rewrites the scope line) — the decision text is stale and should say so.

Consequently the Coverage section's closure claim — *"Items no part owns are exactly the Decisions
above marked 'no task' or 'follow-up' (D4, D9 until 20.9b is added, D21, D23)"* — is **false**. Add
rows D24-D31 for the items above, and rewrite the Coverage sentence to enumerate the real set.

### MAJOR-12 — D22's stated consumer does not cover the file it names
D22: *"`generate_sparsedata_user.py` shares `backfill_agents.py`'s numeric-branch bug — default: **fix
in 26.11**"*. Part 26's decision #1 says the opposite: *"Neither the issue's C1/C2 items nor the
coordinator's DOC-C guidance names this file for the fallback fix — only its `_extract_json` import
(Task 26.12) is in scope here."* Task 26.11's Files list does not include
`scripts/generate_sparsedata_user.py`. So approving the default changes nothing.

**Corrected D22 row:** `… default **yes — extend Task 26.11's Files list with
scripts/generate_sparsedata_user.py — _resolve_agent_id (:165-183) and apply the identical
`f"{prefixed}{i}"` fix + the same unit test**` and add that file/symbol to Task 26.11's Files list.

### MAJOR-13 — Five cross-part file/function overlaps the reconciliation section does not list
Built by parsing every `Modify:`/`Create:` line into a file→tasks map. Reconciliation covers
`review_proposal`, `_sync_roster_from_db`, the `make_engine` files, `CLAUDE.md`, and `agent_page.py`
function ownership. It does **not** cover:

| File | Parts / tasks | Overlap | Order OK? |
|---|---|---|---|
| `src/services/orcid.py` + `tests/contract/test_orcid_contract.py` | 23.11 (phase 2) → 22.1 (phase 5) | **Same functions**: 23.11 edits `fetch_orcid_record`, `fetch_orcid_grants`, `fetch_orcid_works`; 22.1 edits `fetch_orcid_profile`, `fetch_orcid_grants`, `fetch_orcid_works`. Both modify the same contract test file. All four names verified to exist (`orcid.py:13, 23, 76, 98`). | Yes (23.11 first) — but 22.1's `:23-137` anchors and its BEFORE snippets are written against 18ba52c and will be stale. |
| `src/routers/invite.py` | 22.14 → 23.15 | **Same handler**: 22.14 rewrites the `delegate_slack_ids` append at `:241-244`; 23.15 adds a log line in the Slack-sync block of the *same* accept-invitation handler. Both extend `tests/integration/test_agent_page.py`. | Yes (22.14 first) |
| `src/models/agent_activity.py` — class `ThreadDecision` | 25.3 (phase 3) → 20.10 (phase 3) | 25.3 adds `__table_args__` to `ThreadDecision` (which has none); 20.10 adds the `reopened_at` column "right after `refined_in_channel` (:234-237 today)". 20.10's `:209-240` anchors shift. | Yes, but 20.10 must re-anchor by symbol. |
| `src/main.py` | 25.4 → 27.2 | 25.4 edits `AgentBadgeMiddleware.dispatch` (:28-32); 27.2 edits the imports at `:6`/`:8` and the `health` route at `:150-153`. | Yes |
| `src/routers/admin.py` / `src/routers/profile.py` | 22.9 / 22.7-22.8-22.13 → 25.2 | Disjoint functions (`admin_users` vs `admin_delete_user`; `profile_save` vs `delete_account`). | Yes |

**Corrected text — add as reconciliation items 10-12:**
> 10. **`src/services/orcid.py` + `tests/contract/test_orcid_contract.py`**: 23.11 (retry wrapper on
>     `fetch_orcid_record`/`_grants`/`_works` + autouse fixture) lands in phase 2; **22.1 (null-safe
>     `_get()`) is applied ON TOP** and must re-read all four functions before editing — its
>     `:23-137` anchors and BEFORE snippets are pre-23.11. Re-run
>     `tests/contract/test_orcid_contract.py` in full after 22.1.
> 11. **`src/routers/invite.py`** accept-invitation handler: 22.14 (SQL-side `delegate_slack_ids`
>     append) then 23.15 (log the no-token skip). Same handler, adjacent lines; 23.15 re-anchors and
>     re-runs 22.14's `tests/integration/test_agent_page.py` additions.
> 12. **`src/models/agent_activity.py` class `ThreadDecision`**: 25.3 adds `__table_args__`, then
>     20.10 adds `reopened_at`. Disjoint regions of one class; 20.10 re-anchors by symbol. Also
>     `src/main.py`: 25.4 (`AgentBadgeMiddleware.dispatch`) then 27.2 (`health` + imports) —
>     disjoint, sequential.

### MAJOR-14 — Task 27.1 lands first and removes `tests/` from the image, but ~15 later tasks run pytest inside the container without saying which compose file
27.1's `.dockerignore` adds `tests`. Reconciliation item 6 acknowledges this only as a CLAUDE.md
wording change. But many task steps (22.2 Step 2/4, 25.1 Step 2, 25.3 Step 1/2, M.1 Step 4, …) say:
```
docker compose exec -T -e TEST_DATABASE_URL=… app python -m pytest tests/… -v
```
with no `-f` flag. If the executor has followed CLAUDE.md and exported
`COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml`, those commands hit a container
built from an image with **no `tests/` directory** and fail with "file or directory not found".

**Corrected text — add to Global Constraints:**
> Every in-container `pytest` command in this plan targets the **dev** compose file
> (`docker-compose.yml`), which bind-mounts the whole repo at `/app`. Run them as
> `docker compose -f docker-compose.yml exec -T -e TEST_DATABASE_URL=… app python -m pytest …`, or
> `unset COMPOSE_FILE` first. After Task 27.1 the prod image no longer contains `tests/`, so the prod
> file set cannot run them.

### MAJOR-15 — Part R never applies 27.13's resource limits to `postgres` and `certbot`
Task 27.13's Deploy note: *"`docker compose $C up -d` (**not just restart**) is required for
`mem_limit`/`cpus` to apply; this recreates every service, so do it in a maintenance window."*
Part R only ever runs `up -d app worker grantbot` (R.7) and `up -d nginx` (R.8). `postgres` and
`certbot` are never recreated, so their limits silently do not take effect and the deploy log will
claim otherwise.

**Corrected text — add R.7b:**
```bash
# 27.13's mem_limit/cpus only apply on recreate. app/worker/grantbot/nginx are covered by R.7/R.8.
# postgres and certbot are NOT — and recreating postgres restarts the database.
docker compose $C up -d certbot                      # trivial, do it now
docker inspect copi-python-postgres-1 -f '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}'
# If this is 0 0 and you want postgres's limit live, it needs its own short window:
#   docker compose $C stop app worker grantbot && docker compose $C up -d postgres && … R.7 again
# DECISION: default is to leave postgres unlimited until a separate window (record it in the log).
```
and add a Decisions row for it.

### MAJOR-16 — `$ROLLBACK_SHA`, `$DEPLOY_SHA` and `$PRE_DEPLOY_DUMP` are never assigned but are used in executable commands
R.1 says "note the PRE-deploy sha as ROLLBACK_SHA" in prose; R.2 says "Record the dump path as
`PRE_DEPLOY_DUMP`". R.6 then interpolates `$PRE_DEPLOY_DUMP` into the
`--backup-verified-elsewhere` string and R.10 runs `pg_restore … < "$PRE_DEPLOY_DUMP"` and
`git checkout $ROLLBACK_SHA`. A deploying **agent** executing these literally gets
`pg_restore … < ""` (error) and a bare `git checkout` (no-op). Given the rollback path is exactly
where this matters, make them real:

**Corrected R.1 / R.2:**
```bash
# R.1
export ROLLBACK_SHA="$(git rev-parse HEAD)"        ; echo "ROLLBACK_SHA=$ROLLBACK_SHA"
export DEPLOY_SHA="$(git rev-parse origin/copi-prod)" ; echo "DEPLOY_SHA=$DEPLOY_SHA"
# R.2, after the backup run
export PRE_DEPLOY_DUMP="$(sudo ls -1t /var/backups/copi/copi-python/*.dump | head -1)"
sudo test -s "$PRE_DEPLOY_DUMP" && echo "PRE_DEPLOY_DUMP=$PRE_DEPLOY_DUMP"
```
(Note `/var/backups/copi` is `0700 root` — `install.sh:26` — so both the `ls` in R.2 and the
`pg_restore … < "$PRE_DEPLOY_DUMP"` redirection in R.10 need `sudo`; the redirection in particular
must be `sudo sh -c 'docker exec -i copi-python-postgres-1 pg_restore … < "$0"' "$PRE_DEPLOY_DUMP"`
or the dump must first be `sudo cp`'d somewhere readable.)

### MAJOR-17 — M.2's test has four ruff violations and cannot verify the behaviour it claims
`pyproject.toml:68` selects `["E","F","I","UP","B"]` (only `E501` ignored) and `ci.sh` runs
`ruff check` over `tests/` as a hard gate. Measured with the repo's own ruff on the plan's snippets:
```
E401 Multiple imports on one line        (import os, subprocess, pathlib, stat)
I001 Import block is un-sorted
F401 `pathlib` imported but unused
F841 Local variable `r` is assigned to but never used
```
M.1's snippet adds two more: `E401`+`I001` on `import re, pathlib` inside the function, and — because
the plan says to insert `from scripts.migrate import preflight as pf` "near the existing
REVISION_ORDER tests (~:232)" — **`E402` module-level import not at top of file**, plus it would
rebind the module-level `pf` that `_load()` already created at the top of the file (a *second* copy of
preflight loaded under a different module name).

(Checked, and it is *not* a blocker: `from scripts.migrate import preflight` **does** work — `scripts/`
has no `__init__.py` but implicit namespace packages resolve it, and `tests/__init__.py` +
`tests/unit/__init__.py` exist so pytest's prepend importmode puts the repo root on `sys.path`.
Verified: `.venv-test/bin/python -c "from scripts.migrate import preflight as pf; print(pf.DEFAULT_TARGET)"` → `0024`.)

Also, the shim's `*"ps --status running --services"*` case **echoes `app`**, so `grep -qx app` succeeds
either way — the test cannot detect a failure to skip the running-service assertion, which is half of
what `--via-run` is for.

**Corrected M.1 Step 1** — do not add a new import; the file already has `pf`/`po`. Put the tests
next to `test_revision_order_covers_the_supported_range` (`:952`):
```python
def test_revision_order_ends_at_the_repo_head():
    import pathlib
    import re

    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    heads = []
    for p in sorted(versions.glob("0*.py")):
        m = re.search(r'^revision(?::\s*str)?\s*=\s*"(\d{4})"', p.read_text(), re.M)
        assert m, p
        heads.append(m.group(1))
    assert pf.REVISION_ORDER[-1] == max(heads)
    assert pf.DEFAULT_TARGET == pf.REVISION_ORDER[-1]


def test_0024_is_a_supported_start_because_org1_sits_there():
    assert "0024" in pf.SUPPORTED_START_REVISIONS


def test_new_chain_objects_are_planned():
    names = {o.name for o in pf.planned_objects_between("0024", pf.DEFAULT_TARGET)}
    assert "uq_publications_user_pmid" in names            # 0025
    assert "ix_thread_decisions_agent_a_outcome" in names  # 0027 badge composite
    assert "ix_agent_delegates_user_id" in names           # 0027 FK index
    assert "reopened_at" in names                          # 0028
```
(`pathlib` is unused above — drop it; `Path` is already imported at the top of the file.
Verified the anchor works: every migration uses `revision: str = "NNNN"`, e.g.
`0024_add_agent_role.py:21`.)
**Corrected M.2 Step 1** header + assertions:
```python
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_via_run_uses_compose_run_not_exec_and_skips_the_running_check(tmp_path):
    log = tmp_path / "argv.log"
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {log}\n'
        "case \"$*\" in\n"
        '  *"ps --status running --services"*) echo NOT-app ;;\n'   # must be irrelevant under --via-run
        '  *"import src; print(src.__file__)"*) echo /app/src/__init__.py ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DATABASE_URL": "postgresql+asyncpg://copi:x@postgres:5432/copi",
        "MIGRATE_BACKUP_DIR": str(tmp_path / "backups"),
    }
    proc = subprocess.run(
        ["./scripts/migrate/run_migration.sh", "--via-run",
         "--backup-verified-elsewhere", "test"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    argv = log.read_text()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert " run --rm --no-deps -T " in argv
    assert "/migrate-state" in argv, "the snapshot must be on a bind mount (see M.2 Step 3)"
    assert " exec " not in argv.replace("exec -T postgres", "")
```
The shim answering `NOT-app` to `ps` is what makes the test actually prove the skip.
Also fix M.2 Step 2's stated failure: with `--via-run` unknown, `die_usage` exits 64 **before any
docker call**, so the observed failure is `FileNotFoundError: …/argv.log`, not an assertion.

### MAJOR-18 — R.6's DSN is not percent-encoded and R.2/R.10 need `sudo`
R.6: `export DATABASE_URL=postgresql+asyncpg://copi:<POSTGRES_PASSWORD from .env>@postgres:5432/copi`.
If the prod password contains `@`, `/`, `:`, `#` or `%`, asyncpg/SQLAlchemy will mis-parse it and
alembic will connect somewhere unexpected (or fail) — and `run_migration.sh:149` masks it in output,
so the operator will not see the mangling.

**Corrected R.6 first line:**
```bash
PW="$(grep -m1 '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)"
PW_ENC="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PW")"
export DATABASE_URL="postgresql+asyncpg://copi:${PW_ENC}@postgres:5432/copi"
unset PW PW_ENC
# Sanity: this must print a masked DSN whose host is `postgres` and db is `copi`.
./scripts/migrate/run_migration.sh --via-run --backup-verified-elsewhere "dsn check" 2>&1 | grep 'target:'
```
Also add `sudo` to R.2's `ls -la /var/backups/copi/copi-python/` (0700 root) and to R.10's dump read.

---

## 4. MINOR findings

1. **Part M's preamble contradicts the binding reconciliation.** "`HEAD_REVISION` below is `0027`
   unless Part 20 ships `0028`" — item 1 already fixes it at `0028`. Delete the conditional and write
   `0028` throughout Part M and R.
2. **M.1 Step 2's expected failure text is placeholder leakage**: `'0024' != 'HEAD_REVISION'`. The
   real message is `assert '0024' == '0028'`.
3. **M.1's postflight anchors are drifted**: `EXPECTED_INDEXES` is `:143-162` (plan says `:141-156`),
   `EXPECTED_CONSTRAINTS` is `:164-173` (plan says `:159-168`). Also `postflight.py:97-180` in the
   preamble should be `:97-184`.
4. **Adding a 14th postflight check** (if B4's simplification is rejected) breaks
   `docs/production-migration.md`'s "13 checks" at `:370`, `:569`, `:575`. With B4's fix, no count
   changes — another reason to prefer it.
5. **M.3 step 1's sentence is garbled**: "Add to the scope line that starts at 0024 are supported".
   Also `docs/production-migration.md`'s **title** (line 1: "Production migration to alembic 0024")
   drifts and is not in M.3's Files list.
6. **`docker compose ps` hides exited containers** in compose v2 — R.7's "migrate: Exited (0)"
   needs `docker compose $C ps -a`.
7. **R.4's `docker stop -t 30 agent-run && docker rm agent-run`** short-circuits if `agent-run` is
   absent. Use `|| true` on both (as the `docker logs` line already does).
8. **R.9's `docker logs -f … | grep`** needs `--line-buffered` or the operator sees nothing for
   minutes; the plan's grep line also has trailing whitespace.
9. **R.9's `\d private_channel_members | grep -A1 user_id_fkey`** matches both
   `private_channel_members_user_id_fkey` (CASCADE) and `private_channel_members_added_by_user_id_fkey`
   (SET NULL). State both expectations, or use
   `psql -c "select conname, pg_get_constraintdef(oid) from pg_constraint where conrelid='private_channel_members'::regclass and contype='f'"`.
10. **R.1's "3× the database size free" STOP condition has no measuring command** — `df -h
    /var/lib/postgresql/data` shows the volume, not the DB. Add
    `psql -c "select pg_size_pretty(pg_database_size('copi'))"`.
11. **R.8's `up -d nginx` also (re)runs `migrate`** now that `app` depends on it (nginx →
    `depends_on: app: service_healthy` → app → migrate). Idempotent, but note it so the operator is
    not surprised, and so a broken `migrate` is recognised as the cause of a failed nginx start.
12. **R.10 must warn not to run any `up -d` between the restore and the `git checkout $ROLLBACK_SHA`** —
    the new tree's `migrate` service would immediately re-apply 0025-0028 to the just-restored 0024 DB.
    Also `git checkout $ROLLBACK_SHA` leaves a detached HEAD and an orphaned `copi-python-migrate-1`
    container (harmless; do **not** add `--remove-orphans`, CLAUDE.md:106).
13. **Reconciliation item 6 states the CLAUDE.md order backwards.** 27.1 is phase 1 (first commit of
    the branch) and 26.7 is phase 5, so the sequence is 27.1 → 26.7, not "26.7 … and 27.1".
14. **Execution-order note "27.3 depends on M.1 (head pin)" is spurious** — 27.3's `migrate` service
    runs `alembic upgrade head`, which pins nothing. 27.3's only real dependency is that the
    override-file fix (B1) lands with it.
15. **D15's parenthetical is inaccurate**: "`uv` and pip-tools are not installed". Per MASTER_PLAN
    L17390 `uv` *is* at `~/.local/bin/uv` (just not in `.venv-test/bin`), and L18064/L18134 both
    *use* `uv pip install … pip-tools`. Say "pip-tools is not installed in `.venv-test`; install it
    with the host's `uv`".
16. **`Opening the PR`'s `MIGCHECK_PORT=55433` is prose, not an env prefix.** Write
    `MIGCHECK_PORT=55433 git push -u origin close-issues-20-27`.
17. **Only 6 ruff findings of headroom.** Verified: `ruff check src/` → `Found 254 errors`, ceiling
    `SRC_LINT_MAX=260` (`ci.sh:55`). Across 106 tasks adding code to `src/`, six is thin — add a
    Global Constraint that every new/edited `src/` line must be ruff-clean under `E,F,I,UP,B`, and
    note that raising `SRC_LINT_MAX` is **not** permitted by this plan.
18. **`--via-run` costs one container start per in-container step** (2 import checks + preflight +
    alembic + the revision read-back + postflight = 6). On a 3.7 GB host that is fine but slower
    than `exec`; and under `set -e` a failing `SRC_PATH="$(docker compose run …)"` kills the script
    before its own BLOCKED message prints (pre-existing at `:114`, but `--via-run` makes the failure
    more likely). Suggest `| tail -n 1` on the Step 1 and Step 6 captures in case `docker compose run`
    ever emits progress on stdout — worth confirming during R.6's rehearsal, because a stray line
    would make Step 6 declare a *silent rollback* after a successful migration.

---

## 5. Anchor mismatches (consolidated)

| Plan says | Reality | Verdict |
|---|---|---|
| `preflight.py` `DEFAULT_TARGET (:74)` | `:74` | OK |
| `preflight.py` `SUPPORTED_START_REVISIONS (:89)` | `:89` | OK |
| `preflight.py` `PLANNED_OBJECTS (:168-204)` | `:168-204` | OK |
| `preflight.py` `REVISION_ORDER (:206)` | `:206` | OK |
| `preflight.py` `POST_0019_STARTS` | `:93` — **not in M.1's Files list** | missing |
| `postflight.py` `EXPECTED_INDEXES (:141-156)` | `:143-162` | drifted |
| `postflight.py` `EXPECTED_CONSTRAINTS (:159-168)` | `:164-173` | drifted |
| `postflight.py` `EXPECTED_COLUMNS` | `:97-131` — **not in M.1's Files list** | missing |
| `postflight.py` `EXPECTED_* (:97-180)` (preamble) | `:97-184` | drifted |
| `run_migration.sh` header `:3` / `TARGET :56` | `:3` / `:56` | OK |
| `run_migration.sh` flags `:67-88`, Step 1 `:107-128`, `run_py() :151-153`, Step 5 `:251-266`, footer `:312-326` | all exact | OK |
| `test_migration_checks.py` "0024" pins `:232, :838, :1129, :1170` | five hits: `:223, :232, :838, :1129, :1170`; plus the tuple at `:231` | **incomplete** |
| `test_migration_checks.py:838` described as a "0024 literal" | it is the drift-guard's revision **tuple** | **wrong semantics (B6)** |
| `test_harness_smoke.py:15` | `:15` (`assert v == "0024"`) | OK |
| `docker-compose.prod.yml` app `depends_on (:42-44)` | `depends_on:` at `:41`, body `:42-44` | OK |
| deploy dossier §7 "Roster sync … simulation.py:381" | `:381` is the **cohort** log-signature comment; the roster lines are `[roster] …` at `:4574+` | **dossier + R.9 wrong** |
| CLAUDE.md / R.9 `=== Turn 1 ===` | real format `=== Turn %d: %s ===` (`simulation.py:745`) | **wrong** |

Verified-correct facts (no action): 20-index list in M.1 == 25.3's `_INDEXES` exactly; `"constraint"`
IS a supported `PlannedObject.kind` (`existing_object_names` builds a `"constraint"` set at
`preflight.py:911, 927-932`, and check 5 handles it at `:1418-1422`); none of the 21 new object names
pre-exists in `alembic/` or `src/`; `private_channel_members_{user_id,added_by_user_id}_fkey` are the
real Postgres-assigned names (0011 creates both FKs unnamed); `reopened_at` is
`timestamp with time zone`/nullable, matching the reconciliation's `EXPECTED_COLUMNS` row; postflight
has a clean check-registration pattern (`report.add_guarded(title, lambda: check_x(conn))`,
`postflight.py:687-737`) for M.1 to mirror; `run_migration.sh` calls
`docker compose ps --status running --services` verbatim (`:109`) and reads `DATABASE_URL` (`:57`);
`--no-deps` does defeat 27.3's `depends_on: migrate` and `docker compose run` does support `-T`, `-e`
before the service name, and `-v`; `pg_restore -Fc` from `docker exec -i` stdin is correct (design-doc
precedent) and `--exit-on-error`/`--no-owner`/`--no-privileges`/`-U copi -d copi` are right;
`nginx -t` / `nginx -s reload` via `exec -T nginx` are valid for `nginx:1.27-alpine`;
`copi-backup run --no-prune` exists (`copi_backup.py:1495`) and `status.json` is at
`/var/backups/copi/status.json` (`:1226`); container names `copi-python-<svc>-1` and `agent-run` are
right; the R.9 duplicate-publications SQL is correct (NULL pmids legitimately do not appear);
`/api/health` answers HEAD (Starlette adds it to GET routes) so `curl -I` works through nginx;
"old code on new schema" is right for 0025 (0027/0028 are inert; 0026's CASCADE only makes a
previously-failing user delete succeed, so old code is safe in that direction too); Global Constraints'
figures (254 findings, ceiling 260, COV_MIN 60) all reproduce.

---

## 6. Counts

- **BLOCKER: 6** — B1 (migrate service missing from the logging override → R.7 takes prod down),
  B2 (`--via-run` snapshot loss → postflight FAILs after a good migration → R.10 restore),
  B3 (R.4 leaves app/worker up; contradicts preflight check 7 and the chain's lock footprint),
  B4 (`confdeltype` bytes-vs-str → M.1's new check/test fails on a correct schema),
  B5 (`uq_publications_user_pmid` missing from `EXPECTED_INDEXES` → existing unit test fails),
  B6 (drift-guard tuple bump silently skips 0025-0027).
- **MAJOR: 18**
- **MINOR: 18**
