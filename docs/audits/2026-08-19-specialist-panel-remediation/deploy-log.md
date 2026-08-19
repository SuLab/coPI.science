# Production deploy log — specialist panel remediation (0029)

**Date:** 2026-08-19
**Branch:** `blackbird` @ `a6c34da` (fast-forward merge of `feat/specialist-panel-remediation`, 16 commits)
**Migration:** `0028` → `0029` (`opportunity_assessments.panel_incomplete`, `.missing_domains`)
**Audit:** `post-implementation-audit.md` in this directory

## Pre-deploy state

- Local `blackbird` was 6 commits ahead of `origin/blackbird` (`753653d`) before the merge — 3 were
  this work's design/plan docs, 3 were pre-existing unpushed ops fixes (`fa543ae`, `3e78458`,
  `9e70522`). Push was a fast-forward; nothing was rebased or forced.
- Production DB at `0028`. 18 `opportunity_assessments` rows, 3 `assessment_drops` rows.
- Simulation container `blackbird-agent-run` already `Exited (137)` (SIGKILL, 38h).
- Container census taken before touching anything: **7 `copi-python` containers** (the other,
  unrelated production deployment sharing this host) + 3 `copi-blackbird`.

## Sequence executed

Gate re-run on the merged tree *before* anything was pushed or deployed — not reused from the
feature branch:

```
2071 passed, 93 skipped   coverage 76.13%   16 snapshots passed   ==> CI passed.
```

Then, in this order (the `0028` lesson: the model maps both new columns from this commit on, so
old-schema-with-new-code raises `UndefinedColumn` on every `select(OpportunityAssessment)`):

```bash
DC="docker compose -f docker-compose.prod.yml"     # never bare `docker compose`
$DC build blackbird-app worker                     # 1. build
$DC run --rm blackbird-app alembic upgrade head    # 2. MIGRATE BEFORE SERVING
$DC run --rm blackbird-app alembic current         # 3. verify == heads
$DC up -d blackbird-app worker                     # 4. start new code
$DC --profile agent build agent                    # 5. agent image bakes src/
git push origin blackbird
```

`--remove-orphans` was **never** passed. Compose emitted its usual
`Found orphan containers ([blackbird-agent-run])` warning suggesting that flag; it was ignored,
because per `CLAUDE.md` that flag has previously killed the other deployment's nginx and certbot.

## Verification

| Check | Result |
|---|---|
| `alembic current` / `alembic heads` | both `0029 (head)` |
| Direct DB read of `alembic_version` | `0029` |
| New columns | `panel_incomplete boolean NOT NULL DEFAULT false`, `missing_domains jsonb NULL` |
| Existing rows survived | 18 rows / 18 defaulted `panel_incomplete=false` / 18 `missing_domains NULL` |
| App logs for `UndefinedColumn` / traceback | **none** |
| `copi-blackbird-app-1` | `running`, `health=healthy`, 4× `GET /api/health → 200 OK` |
| `copi-blackbird-postgres-1` | `running`, `health=healthy` |
| `copi-blackbird-worker-1` | `running`, "Worker started, polling every 5s", no errors |
| Images rebuilt | app, worker **and** agent, all timestamped at deploy |

### The other deployment was not disturbed

All 7 `copi-python` containers still running with **byte-identical `StartedAt` timestamps** to the
pre-deploy census — including `nginx` (`2026-08-06T21:55:15`), `certbot` (`2026-08-06T21:55:04`)
and their live `agent-run` (`2026-08-14T15:28:30`). Nothing of theirs was stopped, removed or
restarted.

### The new instrumentation, exercised against real production data

`list_assessments(db, 'all')` run in a one-off container off the deployed image:

```
assessments returned   : 18
incomplete_panel_count : 0          # correct — under the old code a gapped verdict was refused,
band_counts            : [('pass', 18)]   #   so every stored row had cleared the floor
```

Per-dimension distribution reproduced the original audit's findings **from the live instrument**,
which is the point of F8 — these numbers previously required a manual audit to discover:

- `band_counts` is a single bar: all 18 assessments band `pass`. Zero variance.
- Four dimensions never exceed 2 — `external_signals` (mean 1.28), `ip_fto` (1.33),
  `exit_thesis` (1.56), `chemistry_dc_path` (1.56) — pinning 23 of 100 weight points near minimum.
- `maps_to_dimension` resolves for all 8 specialists (`commercial→differentiation`,
  `legal→ip_fto`, `chemistry→chemistry_dc_path`, …); the 5 unmapped rubric dimensions render blank.

## Not done, deliberately

- **The simulation was not started.** `blackbird-agent-run` remains `Exited (137)`. The agent image
  is rebuilt and ready, but starting a run makes real Opus calls and is an operator decision.
- **The clear-rate monitor will not fire until a run happens and exits gracefully.** It is a
  shutdown-time log warning, and this container's last exit was a SIGKILL, which skips it. Do not
  read "no warning" as evidence of a healthy panel. See the audit's §6 and spec §10 for the tracked
  follow-up (a DB-backed card off `llm_call_logs WHERE phase LIKE 'consult%'`).

## Outstanding hazard

The unmerged branch `feat/user-account-types-0029` also claims revision `0029`
(`0029_drop_is_admin.py`). Ours is now applied in production. Whoever merges that branch must
renumber it to `0030` — which `CLAUDE.md` now reserves. `ci.sh`'s single-head check fails loudly on
collision, so it cannot land silently.
