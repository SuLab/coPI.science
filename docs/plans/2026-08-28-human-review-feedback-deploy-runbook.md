# Human review & feedback — deploy runbook

**Companion to:** `.superpowers/sdd/2026-08-28-human-review-feedback-implementation-plan/`
(15 tasks, `feat/human-review-feedback`, 15 commits `b9725ac..6542b8e`, merged
after full CI (see `task-15-report.md` for the gate evidence: single alembic
head `0039`, clean round trip to `0018` and back, zero ruff findings on the
test suite, 224/231 on the `src/` ratchet, 3169 passed / 93 skipped / 0
failed, 83.30% coverage against a 60% floor)).

**Status:** ready to execute. Deploy and the simulation restart are both
operator-gated — this document does not perform either.

This runbook follows CLAUDE.md's canonical "Before restarting" sequence
verbatim, not a lighter one, for two reasons: migration `0039` takes an
ACCESS EXCLUSIVE lock on `users` (it drops and recreates
`ck_users_user_role` to add the `reviewer` value), and the **agent** image
must be rebuilt regardless, for Task 8's engine re-point (`src/agent/
simulation.py::_retire_superseded_verdict` now re-points `AssessmentReview`,
`AssessmentReviewEvent`, `AssessmentReviewAssignment` and
`PromptChangeSuggestion` rows onto the replacement assessment id instead of
losing them when a provisional verdict is superseded).

```bash
DC="docker compose -f docker-compose.prod.yml"
```

## 1. Stop the running simulation, save logs, remove the container

Verbatim CLAUDE.md steps 1-2. The agent is stopped only so the deploy can
proceed safely — this runbook does NOT start a new run afterward; see step 6.

```bash
docker stop -t 420 blackbird-agent-run
docker inspect blackbird-agent-run --format 'exit={{.State.ExitCode}}'
```

Confirm graceful shutdown by the LOG LINE, not the exit code:

```bash
docker logs blackbird-agent-run 2>&1 | tail -50
```

must show `Simulation stopping...` as `SimulationEngine.stop()`'s last
statement — that is what confirms the buffered flush ran (per CLAUDE.md,
exit 137 does not by itself mean data was lost; the log line is the actual
evidence).

```bash
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f
docker rm blackbird-agent-run
```

## 2. Build the web tier and the agent image

Both bake `src/` in; `worker` mounts only `./profiles` and `./prompts:ro`,
never `src/`, so it does not need this step to pick up code — see step 4 for
why it still needs a recreate.

```bash
$DC build blackbird-app worker
$DC --profile agent build agent
```

## 3. Migrate — from a one-off container, before anything serves

`0039` is additive in the sense that old code against the new schema stays
safe (the four new tables are simply unused, and the widened `job_type_enum`
value sits inert until the worker writes one). The reverse is not: the new
web code maps the four new tables via `review_columns_for` — read by BOTH
assessment list pages (`/admin/assessments`, `/manager/assessments`) — and
via the human-review card on BOTH assessment detail pages. Against a
pre-`0039` database every one of those raises `UndefinedTable` site-wide on
the assessment surfaces. Migrate before the new code serves:

```bash
$DC run --rm blackbird-app alembic upgrade head
```

Verify with BOTH of the following — they must agree:

```bash
$DC run --rm blackbird-app alembic current   # must print 0039 (head)
$DC exec -T postgres psql -U copi -d copi -t -A -c \
  "SELECT version_num FROM alembic_version;"  # must equal 0039
```

## 4. Recreate the web tier and the worker

```bash
$DC up -d --force-recreate blackbird-app worker
```

`--force-recreate` is the documented recreate form (CLAUDE.md's `.env` box:
`env_file` is resolved at container creation, so a bare restart would keep a
stale environment). The worker specifically needs the recreate here for its
new `./prompts:/app/prompts:ro` bind mount, and to pick up any
`LLM_REVIEW_MODEL` value set in `.env` (see the note below) — it now imports
`src.services.review_bot` at module top (see step 5).

## 5. Post-deploy checks

- `/admin/assessments` and `/manager/assessments` render the two new columns
  (Assigned / Reviewed-by) from `review_columns_for`.
- Submit one `log_only` feedback on an assessment's human-review card and
  confirm it records without enqueuing a job.
- Submit one `learn` feedback and watch `/admin/jobs` until the
  `review_feedback_analysis` job reaches `completed` (or `dead` after 3
  attempts — if it dies, read `last_error` on the job row before concluding
  the deploy itself is broken).
- Open `/manager/prompt-suggestions` and confirm it renders (empty is
  expected immediately after deploy), then confirm the completed job's
  suggestion appears there and on its detail page
  (`/manager/prompt-suggestions/{id}`).
- `docker logs` the **worker** for a clean start: it now imports
  `src.services.review_bot` at module top, so a broken import fails loudly
  HERE, at startup — not silently at the first `review_feedback_analysis`
  job.
- Enum check:
  ```bash
  $DC exec -T postgres psql -U copi -d copi -c \
    "SELECT unnest(enum_range(NULL::job_type_enum));"
  ```
  must list `generate_profile`, `monthly_refresh`, `review_feedback_analysis`.

## 6. The simulation restart is a separate, operator-gated decision

This runbook stops the agent in step 1 but does not start a new run — that
is what activates Task 8's engine re-point, and starting a run is the
operator's call, per the standing "never auto-start the simulation"
preference.

Until the simulation is restarted: supersession deletes still eat reviews
recorded against provisional (non-terminal) verdict rows — a review attached
to a provisional `opportunity_assessments` row is lost if a later sidecar in
the same interview supersedes it, the same way specialist notes were lost
before this feature existed. This is known, accepted, and bounded: with no
simulation run live while the agent container is stopped, no new provisional
verdicts — and therefore no newly-at-risk reviews — can be created in that
window. The exposure resumes only once a run is restarted.

## `.env` note: `LLM_REVIEW_MODEL`

`LLM_REVIEW_MODEL` is optional and defaults to `claude-opus-5`
(`src/config.py`, `settings.llm_review_model`) when unset. If you do set it,
the worker needs the `--force-recreate` in step 4 to pick it up — env_file
resolution happens at container creation, not on a bare restart.

---

**Do NOT auto-start the simulation — operator-gated.**
