# alembic/ — migrations

Loads when you read files here. Full detail: `docs/production-migration.md` (the
guarded runbook) and `docs/operations/migration-deploy-notes.md` (one deploy note per
migration, with the hazard each one carried).

- Nothing migrates the production database for you. The deploy order is always: build,
  then migrate from a one-off container off the NEW image
  (`./scripts/migrate/run_migration.sh`, rehearsed without `--apply` first), then
  `up -d`. Old code on the new schema is the safe direction, so keep migrations
  additive and nullable. New code that maps a missing column raises `UndefinedColumn`.
  On the engine's best-effort write paths that failure is swallowed into one ERROR
  line, so verdicts are silently lost.
- Commit a new revision before building: the builder stage's `git clean -ffdx` drops
  an untracked `alembic/versions/*.py` from the image.
- A new head also moves `scripts/migrate/preflight.py`: `DEFAULT_TARGET`,
  `REVISION_ORDER`, the previous head joining `SUPPORTED_START_REVISIONS`, and
  `PLANNED_OBJECTS` (plus `PLANNED_DROPS` for a drop). The pins in
  `tests/unit/test_migration_checks.py` and `tests/integration/test_harness_smoke.py`
  move with it.
- Add the migration's deploy note to `docs/operations/migration-deploy-notes.md`, as a
  box that opens "Deploy order for" and the backticked revision slug. Say what old code
  on the new schema and new code on the old schema each do, and whether the agent image
  must be rebuilt with it. `tests/unit/test_claude_md_references.py` checks that every
  box names a migration that exists.
- NULL in a new column means "never asked", not "no". Do not backfill it with a guess.
- Postgres cannot drop an enum value: a downgrade leaves added `job_type_enum` values
  in place.
- `./scripts/ci.sh` runs an upgrade→downgrade→upgrade round trip and fails on a second
  head or a duplicate revision id.
