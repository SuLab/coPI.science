# Blackbird pitch-only reconciliation — deploy checklist

**Companion to:** `docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md` §13
(this document supersedes and expands that section — the design's §13 is the
one-paragraph-per-item summary; this is the executable version, current as of
branch 2 `blackbird-engine-reconciliation`).

**Status:** execution deferred — no live run is in flight (design §2). This is
written to be run once branch 2 is green and both PRs have landed.

**A note on naming before you copy anything below:** every command uses a
`$WEB_SVC` variable for the FastAPI/uvicorn web tier instead of a literal
service name. Resolve it once, first:

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC config --services
```

CLAUDE.md's runbook (and every prior deploy doc in this repo, e.g.
`docs/plans/2026-08-06-role-topology-post-type-gating.md`) calls this service
`blackbird-app`, and that naming was stated as "verified against the running
stack" (commit `996cca7`). The `docker-compose.prod.yml` **checked into this
git worktree**, however, declares the service key as plain `app` — confirmed
with `docker compose -f docker-compose.prod.yml --profile agent config
--services` → `postgres worker agent app certbot nginx`, no `blackbird-app` at
any point in this file's git history. The most likely explanation is that the
actual EC2 host's compose file has a local, uncommitted rename that the
git-tracked copy never picked up — this is exactly the kind of repo/production
drift this project's other runbooks warn about, not a new problem this branch
introduces. **Do not guess: run the `config --services` line above against the
target host and set the variable accordingly:**

```bash
WEB_SVC=blackbird-app   # if that's what config --services printed
# or
WEB_SVC=app             # if it printed plain `app`, matching this checkout
```

`worker`, `agent`, `postgres`, `nginx`, `certbot` are not ambiguous — both
CLAUDE.md and the checked-in compose file agree on those five (`certbot` is
usually not counted in "services" prose since nobody execs into it; the
conversational count is the five: postgres/app/worker/agent/nginx).

---

## 0. Merge order

Per design §3:

1. **PR #34** (`blackbird-prompt-refactor` → `blackbird`) merges first. It is
   prompt/doc text only; `./scripts/ci.sh` is red on this branch **by design**
   (17 failing tests) and the PR body already documents that as the known
   mid-state — do not try to make it green before merging it.
2. **The engine PR** (`blackbird-engine-reconciliation`, branched off
   `blackbird-prompt-refactor`, draft-based against it) merges second, after
   `./scripts/ci.sh` is fully green on it and the deep adversarial audit
   (progress ledger's `CONTROLLER ORDER` entry for Task 17) is clean.

Do not attempt any step below until both have landed on whatever branch this
host actually deploys from. If you are staging this on a branch that hasn't
absorbed both merges yet, `git log --oneline -5` and confirm you can see both
the prompt-refactor commits (e.g. `e5055df`, `c9d6cdc`) and the engine commits
(e.g. `8a94611` GrantBot removal, the star-topology validator, the hub
auto-activation commit) before proceeding.

---

## 1. ⚠️ MIGRATION REQUIRED — `alembic upgrade head` now drops a table

Unlike the design's original text ("No migrations are expected from this
design"), this plan **does** introduce one: `alembic/versions/0026_drop_grantbot_posted_foas.py`
drops the `grantbot_posted_foas` table outright (`0026`, `down_revision =
"0025"`). This is destructive and has no reversible seed path — `0026`'s
`downgrade()` recreates the table's 4 columns but does **not** restore rows.

**If you want the FOA-posting history preserved, archive it first:**

```bash
DC="docker compose -f docker-compose.prod.yml"
mkdir -p backups
$DC exec -T postgres pg_dump -U copi -d copi -t grantbot_posted_foas \
  > "backups/grantbot_posted_foas_$(date +%Y%m%dT%H%M%S).sql"
```

(`backups/` is already gitignored in this repo — `.gitignore:88`.)

**Then check where you are before touching anything:**

```bash
$DC exec -T postgres psql -U copi -d copi -t -A -c \
  "SELECT version_num FROM alembic_version;"
$DC exec -T "$WEB_SVC" alembic heads      # confirm 0026 is the only head
```

**Apply — the simple path** (CLAUDE.md's documented default):

```bash
$DC exec -T "$WEB_SVC" alembic upgrade head
$DC exec -T "$WEB_SVC" alembic current    # confirm it now reads 0026
```

**Apply — the guarded path** (`scripts/migrate/run_migration.sh`, for a
populated database — preflight → backup → apply → postflight):

```bash
COMPOSE_FILE=docker-compose.prod.yml MIGRATE_SERVICE="$WEB_SVC" \
  ./scripts/migrate/run_migration.sh --apply --target 0026
```

> ⚠️ **You must pass `--target 0026` explicitly with this script.** Its own
> default is stale: `scripts/migrate/run_migration.sh:56` still hardcodes
> `TARGET="0025"` — it was not bumped alongside `preflight.py`'s
> `DEFAULT_TARGET` in commit `e1ee5bb` (that fix only touched
> `preflight.py`/`postflight.py`, both of which now correctly default to
> `0026`; `run_migration.sh`'s own bash variable was missed). CLAUDE.md's
> documented example invocation of this script (the "guarded path" paragraph
> under "Running the Agent Simulation") does **not** pass `--target`, and would
> therefore silently stop one migration short of head, leaving
> `grantbot_posted_foas` in place — flagged below in "CLAUDE.md updates needed
> at merge."

Nothing else migrates the database — this is the same "nothing migrates for
you" warning CLAUDE.md gives for every prior migration; it applies here too.

---

## 2. Stop the GrantBot compose service; note the CloudWatch orphan

The `grantbot` service is already deleted from `docker-compose.prod.yml` on
this branch (commit `8a94611`) — there is no `grantbot:` key left to `docker
compose stop`. If GrantBot was running on the target host from before this
deploy, its **container** still exists and must be found and stopped directly
(you cannot address it by service name once the new compose file is in play):

```bash
GRANTBOT_CID=$(docker ps -aq \
  --filter "label=com.docker.compose.project=copi-blackbird" \
  --filter "label=com.docker.compose.service=grantbot")

if [ -n "$GRANTBOT_CID" ]; then
  docker inspect "$GRANTBOT_CID" --format '{{index .Config.Labels "com.docker.compose.project"}}'
  # MUST print copi-blackbird before you touch it.
  docker stop -t 30 "$GRANTBOT_CID"
  docker rm "$GRANTBOT_CID"
else
  echo "No grantbot container on this host (already removed, or never deployed here)."
fi
```

**CloudWatch orphan:** `docker-compose.prod.yml`'s old `grantbot` service
logged to `awslogs-group: /copi/grantbot`. Deleting the service does not
delete the log group — it simply stops receiving new streams and becomes
orphaned. Leave it if you want the history; if you're sure you don't:

```bash
aws logs delete-log-group --log-group-name /copi/grantbot --region "${AWS_REGION:-us-east-2}"
```

Treat this exactly like the `pg_dump` archive in §1 — it's a one-way door, get
sign-off before running it, and it's independent of the app-level steps (you
can defer it indefinitely with no functional consequence).

---

## 3. DB purges

### 3a. Legacy unreviewed proposals

Design §7/§8: the `Proposal` model (in code, `ThreadDecision` +
`ProposalReview`) and its table stay for historical data — **do not bulk-delete
reviewed rows.** A `ThreadDecision` with `outcome = 'proposal'` and **zero**
matching `ProposalReview` rows is still what
`SimulationEngine._rebuild_agent_state` (`src/agent/simulation.py:4341`+)
reloads into `agent.state.pending_proposals` on every restart — but the
`unreviewed_proposal_block_count` setting that used to make a 2+ count
permanently block Phase 5 was deleted in the 2026-08-12 removal-cycle
consolidation sweep, once nothing on this branch could create a new
proposal for a PI to review any more (the `:memo:`/`✅` handshake was already
deleted, design §8) and the unreviewed-proposal-blocking mechanism itself was
deleted from `_phase5_new_post` (Task 6 of that cycle) — so there is no
longer a Phase-5-blocking consequence to purging (or not purging) these rows.
They are purged here purely for tidiness/historical-data hygiene, not to
unblock anything:

```bash
DC="docker compose -f docker-compose.prod.yml"

# Preview first:
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT count(*) AS legacy_unreviewed_proposals
    FROM thread_decisions
   WHERE outcome = 'proposal'
     AND id NOT IN (SELECT thread_decision_id FROM proposal_reviews);
"

# Then delete:
$DC exec -T postgres psql -U copi -d copi -c "
  DELETE FROM thread_decisions
   WHERE outcome = 'proposal'
     AND id NOT IN (SELECT thread_decision_id FROM proposal_reviews);
"
```

A `ThreadDecision` that has **any** review (even a partial one — one side
reviewed, the other not) is left alone; it's real historical PI-facing data
covered by design §8's "stay for historical data" ruling.

### 3b. Legacy `:moneybag:` funding threads — close administratively

> ⚠️ **Rehearse this before running it here.** Unlike the rest of this
> checklist, the script below is new logic that has never been run against a
> populated database — rehearse it against a scratch DB (`copi_xN`, per
> CLAUDE.md's scratch-DB instructions) loaded with production-shaped data
> before running it against the real deployment.

These are a **separate** problem from 3a: a `:moneybag:` thread that never
received a `ThreadDecision` row at all (many didn't — funding threads used to
have their own open-to-all participation rule with no forced finalize step)
is, by the same `_rebuild_agent_state` logic, **not** in `closed_thread_ids`
and gets reconstructed as a live `active_thread` on every restart, for
whichever two agents last posted in it. With `is_funding_thread`'s "open to
all" exception removed (commit `24e62c8` — ex-funding threads now follow the
normal 2-party rule) and star-topology cohorts in place, an old lab↔lab
funding thread is now a lab↔lab pairing outside any cohort: it becomes
`grandfathered` (`ThreadState.grandfathered`) rather than rejected outright,
which lets it keep receiving replies "so the conversation can conclude" —
indefinitely, since nothing ever concludes it. Insert a closing
`ThreadDecision` for every such thread so the rebuild stops reviving it:

```bash
DC="docker compose -f docker-compose.prod.yml"

# Preview — count only, read-only:
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT count(*) AS legacy_moneybag_threads_without_decision
    FROM agent_messages m
   WHERE m.thread_ts IS NULL
     AND m.content LIKE ':moneybag:%'
     AND NOT EXISTS (
       SELECT 1 FROM thread_decisions td WHERE td.thread_id = m.message_ts
     );
"

# Apply — via the app's own models, so agent_a/agent_b are derived the same
# way the engine itself derives thread participants:
$DC exec -T "$WEB_SVC" python - <<'PY'
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.database import get_session_factory
from src.models import AgentMessage, SimulationRun, ThreadDecision


async def main():
    sf = get_session_factory()
    async with sf() as db:
        roots = (await db.execute(
            select(AgentMessage.message_ts, AgentMessage.channel_name, AgentMessage.agent_id)
            .where(AgentMessage.thread_ts.is_(None), AgentMessage.content.like(":moneybag:%"))
        )).all()
        existing = {r[0] for r in (await db.execute(select(ThreadDecision.thread_id))).all()}
        run_id = (await db.execute(
            select(SimulationRun.id).order_by(SimulationRun.started_at.desc()).limit(1)
        )).scalar_one_or_none()
        if run_id is None:
            print("No simulation_runs row to attach closures to — aborting.")
            return

        to_close = [(ts, ch, sender) for ts, ch, sender in roots if ts and ts not in existing]
        print(f"{len(to_close)} legacy :moneybag: threads without a thread_decisions row")

        for thread_ts, channel, root_sender in to_close:
            replies = (await db.execute(
                select(AgentMessage.agent_id).where(AgentMessage.thread_ts == thread_ts)
            )).all()
            participants = ([root_sender] if root_sender else []) + [
                a for (a,) in replies if a and a != root_sender
            ]
            agent_a = participants[0] if participants else "unknown"
            agent_b = participants[1] if len(participants) > 1 else agent_a
            db.add(ThreadDecision(
                id=uuid.uuid4(),
                simulation_run_id=run_id,
                thread_id=thread_ts,
                channel=channel,
                agent_a=agent_a,
                agent_b=agent_b,
                outcome="timeout",
                summary_text=(
                    "Administratively closed at pitch-only reconciliation deploy "
                    "(legacy :moneybag: funding thread; GrantBot retired)."
                ),
                decided_at=datetime.now(timezone.utc),
            ))
            print(f"  closing {thread_ts} in #{channel} ({agent_a}, {agent_b})")

        await db.commit()
        print("done")


asyncio.run(main())
PY
```

Review the printed list before it commits (the script prints, then commits in
the same pass — re-run the read-only preview query afterward to confirm the
count dropped to 0 if you want a second confirmation).

### 3c. Legacy posts/messages/channels — via `--fresh`

Covered by the standard fresh-start flag, not a separate purge: `--fresh`
(`src/agent/main.py:160-176`) wipes `agent_messages`, `agent_channels`, and
`pi_dm_messages` in full, while **explicitly preserving** `thread_decisions`
and `proposal_reviews` ("preserving proposals and reviews" — this is why 3a/3b
above are separate, deliberate steps and not subsumed by `--fresh`).
`opportunity_assessments` is untouched either way. This is invoked as part of
the restart in §6 below — do the 3a/3b purges **before** that restart, since
they operate on `thread_decisions`, which `--fresh` does not clear.

### 3d. `interesting_posts` — no purge needed (say so)

`interesting_posts` (design §9) is **not** a database table — it's a field on
the in-memory `AgentState` dataclass (`src/agent/state.py:60`), rebuilt from
Slack/DB history only by the scan/prune loop that fed it. Confirmed by reading
`SimulationEngine._rebuild_agent_state` (`src/agent/simulation.py:4244`+):
it reconstructs `active_threads` and `pending_proposals` from DB tables, but
never touches `interesting_posts` — there is no code path that repopulates it
from persisted state. A plain process restart already gives every agent an
empty `interesting_posts` list; there is nothing in Postgres to delete and no
extra step required here. (This differs from `pending_proposals`, which *is*
rebuilt from `thread_decisions` — that's why 3a is a real, necessary DB
action and this one is not.)

---

## 4. Host-file hygiene — ⚠️ archive-and-diff the stale rubric file BEFORE deploy

**Superseded 2026-08-20: this archive-and-diff has already been done.** See
`docs/audits/2026-08-20-rubric-extraction/blackbird-private-diff.md` — the file is
archived as `profiles/private/blackbird.archived-2026-08-20.md` (untracked,
git-ignored). The rest of this section is kept for its record of what was checked and
why, not as an outstanding task.

**Superseded by the 2026-08-12 removal cycle: do NOT just delete this file.**
The private-instructions mechanism that used to load it —
`Agent.private_profile`, `## Your Private Instructions` injection — is deleted
outright. Nothing in the running process reads `profiles/private/blackbird.md`
at runtime any more; the rubric criteria and the `<assessment_json>` skeleton
now live directly in `prompts/roles/scout_hub/phase4-thread-reply.md`. That
means the old failure mode ("delete without replacing leaves BlackbirdBot with
no private instructions") no longer applies, but a **new** risk replaces it:
`profiles/private/blackbird.md` is untracked (`profiles/**/*.md` is
gitignored — "versioned in database via ProfileRevision, not git",
`.gitignore:32-36`) and may still hold hand-transcribed rubric content —
possibly content that was never migrated into the tracked prompt text this
cycle. Deleting it unread would silently lose that content with no way to
recover it.

**Archive and diff — do this before touching anything else in this section:**

```bash
ls -la profiles/private/blackbird.md   # inspect before touching — check mtime/content
mkdir -p backups
cp profiles/private/blackbird.md "backups/blackbird_private_profile_$(date +%Y%m%dT%H%M%S).md"

# Diff against the tracked rubric section (RUBRIC_WEIGHTS / criteria text is
# now in src/services/blackbird_rubric.py and the <assessment_json> skeleton
# in prompts/roles/scout_hub/phase4-thread-reply.md — there is no single
# tracked file with matching prose, so this is a manual read-through, not an
# automated `diff`):
cat profiles/private/blackbird.md
```

> ⚠️ **Escalate if novel content found.** If the archived file contains
> anything beyond the retired **4-criteria gating contract (including
> Baltimore-location gating)** already superseded by the tracked **3-criteria**
> contract (credible technology source, freedom-to-operate, differentiation —
> `tests/unit/test_thread_guidance.py:48-53`, "Baltimore location gating was
> dropped, `dcc5212`") — e.g. scoring notes, weight rationale, or exemplars
> that never made it into `blackbird_rubric.py` or the prompt — stop and get
> sign-off before proceeding; that content would otherwise be lost with no
> tracked home once the file is removed from the host.
>
> Once archived (and any novel content resolved), the file can be deleted —
> there is no longer an "order matters" restart dependency, because nothing
> reads it at runtime:
>
> ```bash
> rm -f profiles/private/blackbird.md
> ```

---

## 5. Verify cohorts are star-shaped

Design §5 (task: startup star-topology validation): once that lands, `start()`
raises `RuntimeError` and the run refuses to come up at all if any cohort is
not star-shaped ({lab, hub} per lab; no lab↔lab cohort). Check this
**before** you restart, so a bad cohort row doesn't cost you a failed
container start:

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT c.name,
         count(*) FILTER (WHERE a.role = 'pi_lab')   AS lab_count,
         count(*) FILTER (WHERE a.role = 'scout_hub') AS hub_count
    FROM cohorts c
    JOIN cohort_memberships m ON m.cohort_id = c.id
    JOIN agents a ON a.agent_id = m.agent_id
   GROUP BY c.id, c.name
  HAVING count(*) FILTER (WHERE a.role = 'pi_lab') <> 1
      OR count(*) FILTER (WHERE a.role = 'scout_hub') <> 1
   ORDER BY c.name;
"
```

**Empty result = star-shaped, proceed.** Any row returned names a cohort with
either more than one lab, more than one hub, or a lab with no hub at all —
fix cohort membership (admin UI) before restarting. This is a manual,
human-readable proxy for the same thing the code-level validator checks via
`allowed_sender_ids` (design §5's exact rule: "any OTHER pi_lab agent id in
its `allowed_sender_ids` is a violation; a pi_lab agent whose gate contains no
scout_hub agent is a violation").

---

## 6. Rebuild images and restart

Standard graceful restart (per CLAUDE.md's runbook), with **one deliberate
deviation for this deploy**: use `--fresh` this one time, to execute the §3c
purge as part of the restart. Do not make `--fresh` your standing habit for
routine restarts afterward — go back to the plain resume flag.

```bash
DC="docker compose -f docker-compose.prod.yml"

# 1. Save logs
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f

# 2. Stop the old container gracefully (SIGTERM, not SIGKILL)
docker inspect blackbird-agent-run --format '{{index .Config.Labels "com.docker.compose.project"}}'
# MUST print copi-blackbird.
docker stop -t 30 blackbird-agent-run
docker rm blackbird-agent-run

# 3. Rebuild the web tier AND the agent image (src/ is baked into both)
$DC up -d --build "$WEB_SVC" worker
$DC --profile agent build agent

# 4. Migration — already done in §1. Confirm it stuck:
$DC exec -T "$WEB_SVC" alembic current   # must read 0026

# 5. Start the new run — --fresh, this deploy only
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh
```

Never pass `--remove-orphans` (kills org1's nginx/certbot on this shared
host).

---

## 7. Verification (full signal set)

Design §13.6 plus this plan's additions. Scope every `llm_call_logs` /
`agent_messages` query below to the **new** run
(`simulation_runs` row created by `--fresh`, i.e. the one with the latest
`started_at`) — old runs will have plenty of historical `scan`/`prune`/
`:moneybag:` rows and that's expected; the new run must have none.

```bash
DC="docker compose -f docker-compose.prod.yml"
RUN_SQL="SELECT id FROM simulation_runs ORDER BY started_at DESC LIMIT 1"
```

**7.1 — A lab's logged phase-5 prompt shows a pitch-only menu.**

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT l.id, l.agent_id, l.created_at, left(l.response_text, 200) AS preview
    FROM llm_call_logs l
    JOIN agents a ON a.agent_id = l.agent_id
   WHERE l.phase = 'new_post' AND a.role = 'pi_lab'
     AND l.simulation_run_id = ($RUN_SQL)
   ORDER BY l.created_at DESC LIMIT 5;
"
```

Confirm the rendered menu in `system_prompt`/`messages_json` offers only the
`:bulb:` pitch option — no `paper`/`help_wanted`/`introduction`/
`idea_crosslab`/`funding_collab` letters.

**7.2 — Hub auto-activation observed on an untagged pitch.** Post (or wait
for) one lab pitch with no `@BlackbirdBot` mention in its body, then:

```bash
docker logs blackbird-agent-run 2>&1 | grep "Auto-activated interview thread"
```

Expect a line matching `Phase 3: Auto-activated interview thread %s (lab post
by %s)` (design's Task 9 exact log text) for that thread, and confirm the hub
actually replied in it — **with `thread_ts` NOT NULL**, i.e. a reply, never a
top-level post (2026-08-12 removal cycle: the hub is hard-gated out of Phase 5
entirely and has no top-level post type left; see §7.7 below for the
run-wide version of this check):

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT thread_ts, agent_id, left(content, 80)
    FROM agent_messages
   WHERE agent_id IN (SELECT agent_id FROM agents WHERE role = 'scout_hub')
   ORDER BY posted_at DESC LIMIT 5;
"
```

**7.3 — One full pitch → interview → assessment loop completes, and the
`opportunity_assessments` row persists FROM THE HUB'S CONCLUDING REPLY** (not
from a separate top-level post — Option A, 2026-08-12 removal cycle: the
`<assessment_json>` sidecar is extracted from the hub's Phase-4 CONCLUDE reply
and stripped before Slack ever sees it).

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT id, agent_id, subject_agent_id, recommendation, band, weighted_score, created_at
    FROM opportunity_assessments
   ORDER BY created_at DESC LIMIT 5;
"
```

Confirm a row exists for the pitch you watched, `band` is one of
`advance|conditional|pass` (computed by `src/services/blackbird_rubric.py`,
never taken from the model), and `gating` is tri-state (`met`/`not_met`/
`unconfirmed`), never a boolean. Then confirm the Slack-visible side of that
same reply carries the verdict prose but NOT the raw sidecar:

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT thread_ts, left(content, 400)
    FROM agent_messages
   WHERE agent_id IN (SELECT agent_id FROM agents WHERE role = 'scout_hub')
     AND thread_ts IS NOT NULL
   ORDER BY posted_at DESC LIMIT 5;
"
```

`content` must NOT contain `<assessment_json>` or a bare `{` JSON blob — it is
stripped before the row is written, same as before Slack ever sees it.

**7.4 — Zero funding activity.**

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT count(*) FROM agent_messages
   WHERE content LIKE ':moneybag:%'
     AND simulation_run_id = ($RUN_SQL);
"
docker ps --filter "label=com.docker.compose.service=grantbot"   # must be empty
```

Both must be zero/empty.

**7.5 — Zero phase-2 LLM calls.**

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT phase, count(*) FROM llm_call_logs
   WHERE simulation_run_id = ($RUN_SQL)
   GROUP BY phase
   ORDER BY phase;
"
```

The result set must contain no `scan` or `prune` rows. As of the 2026-08-12
removal cycle this is not merely "code-dormant" — `_phase2_scan_filter`/
`_phase2_prune`, `build_phase2_scan_prompt`/`build_phase2_prune_prompt`/
`build_scan_system_prompt`, and the `interesting_posts` field they fed are
deleted outright, so there is no code path left that could ever produce a
`scan`/`prune` row on a fresh run. Every other phase (`new_post`,
`thread_reply`, `memory`, etc.) is expected and fine.

**7.6 — Lab capped at one pitch/day.**

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT a.agent_id, to_timestamp(m.posted_at)::date AS post_day, count(*)
    FROM agent_messages m
    JOIN agents a ON a.agent_id = m.agent_id
   WHERE m.thread_ts IS NULL AND a.role = 'pi_lab' AND m.content LIKE ':bulb:%'
     AND m.simulation_run_id = ($RUN_SQL)
   GROUP BY 1, 2
  HAVING count(*) > 1;
"
```

Must return no rows. Corroborate operationally by watching a lab that has
already pitched today hit the cap without an LLM call:

```bash
docker logs blackbird-agent-run 2>&1 | grep "Phase 5: Skipped (daily cap"
```

**7.7 — Zero hub top-level posts, run-wide.** (2026-08-12 removal cycle: the
hub is hard-gated out of Phase 5 — `role.toml` declares `post_types = []` —
every hub message in the run must be a reply, never a root.)

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT count(*) FROM agent_messages m
    JOIN agents a ON a.agent_id = m.agent_id
   WHERE a.role = 'scout_hub'
     AND m.thread_ts IS NULL
     AND m.simulation_run_id = ($RUN_SQL);
"
```

Must be zero. Corroborate operationally — the hub should never log a Phase-5
new-post attempt at all:

```bash
docker logs blackbird-agent-run 2>&1 | grep -i "blackbird.*Phase 5"   # expect no output
```

**7.8 — A cap-reaching interview ends with a CONCLUDE reply, not a bare
timeout.** Pins the audit-discovered ordinal fix (`55822a4`, see the design
doc's addendum): a thread that reaches the structural CONCLUDE point (11
existing messages -> its 12th reply is ordinal 12) must actually receive that
12th, verdict-bearing reply before any close — never a `ThreadDecision`
`outcome = 'timeout'` with the hub silently sitting at 11 messages and no
verdict ever generated.

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT td.thread_id, td.outcome, td.decided_at,
         (SELECT count(*) FROM agent_messages m
           WHERE m.thread_ts = td.thread_id OR m.message_ts = td.thread_id) AS message_count
    FROM thread_decisions td
   WHERE td.simulation_run_id = ($RUN_SQL)
     AND td.outcome = 'timeout';
"
```

For every row returned, confirm `message_count >= 12` (i.e. the thread
actually received its 12th, CONCLUDE-guided reply — with a verdict, checked
in §7.3 — before the *next* turn's system-enforced-close fired) rather than
closing at 11 with no verdict ever attempted. Also check the warning added by
this cycle's consolidation sweep never fires on a healthy run:

```bash
docker logs blackbird-agent-run 2>&1 | grep "no persistable <assessment_json>"
```

Any hit here is a concluded, non-decline hub reply that produced nothing
persistable — worth investigating even if it doesn't block the deploy.

**7.9 — Zero PI flows.** Human-PI-to-bot interaction is retired outright
(2026-08-12 removal cycle): no PI DM directive is ever acted on, and no
inbound-email "instruction" reply ever posts to a thread or migrates a
channel.

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT count(*) FROM pi_dm_messages
   WHERE simulation_run_id = ($RUN_SQL) AND direction = 'inbound';
"
```

Any inbound rows here are durable history only (the web dashboard's PI-DM
route that used to write them is deleted) — confirm no corresponding hub/lab
behavior change followed one, i.e. nothing in `llm_call_logs` around that
timestamp references the DM content. Also confirm no NEW `collab_private`
channel was created this run (the migration flow that used to do so,
`src/services/private_channels.py`, is deleted outright):

```bash
$DC exec -T postgres psql -U copi -d copi -c "
  SELECT count(*) FROM agent_channels
   WHERE simulation_run_id = ($RUN_SQL) AND visibility = 'collab_private';
"
```

Must be zero on a fresh (`--fresh`) run — any pre-existing `collab_private`
channels from a prior run are legacy-tolerance only (decision 8) and are not
what this check is about.

---

## CLAUDE.md updates needed at merge

1. **`retrieve_foa` bullet is now false, not just stale.** CLAUDE.md's
   "BlackbirdBot" section (lines 264–267) still reads: *"`retrieve_foa` is
   withheld from this role by `prompts/roles/scout_hub/role.toml`'s tool
   allow-list."* This branch deletes the `retrieve_foa` tool entirely (design
   §7 — `roles.py:27`'s `DEFAULT_TOOLS` entry and the tool implementation both
   go). There is nothing left to "withhold" — the tool doesn't exist for any
   role. Confirmed: `grep -rn retrieve_foa src/ prompts/` returns zero hits
   once branch 2 lands. This bullet needs to be deleted or replaced with a
   line noting the funding/FOA surface was retired in the pitch-only
   reconciliation.

2. **The guarded-migration example is now unsafe without a flag.** CLAUDE.md's
   "Nothing migrates the database for you" section shows: `COMPOSE_FILE=... 
   MIGRATE_SERVICE=blackbird-app ./scripts/migrate/run_migration.sh --apply`
   with no `--target`. As of this branch, that invocation silently stops at
   `run_migration.sh`'s stale hardcoded default (`TARGET="0025"`,
   `scripts/migrate/run_migration.sh:56`), one migration short of the real
   head (`0026`). Either fix the script's default (bump it alongside
   `preflight.py`'s, which was already corrected in `e1ee5bb`) or add
   `--target 0026` (and future heads) to CLAUDE.md's example. This is a latent
   bug independent of this branch — `e1ee5bb`'s fix touched only
   `preflight.py`/`postflight.py` — but this deploy is what makes it bite.

3. **Not introduced by this branch, but worth reconciling while you're in
   here:** CLAUDE.md and every prior deploy doc call the web service
   `blackbird-app`; the `docker-compose.prod.yml` checked into this worktree
   calls it `app`. See the callout at the top of this document. Whoever owns
   the production host's actual compose file should either commit the real
   service name back to git, or confirm the docs are simply wrong and fix
   them — right now neither this file nor CLAUDE.md can be taken fully at
   face value on this one point, which is why every command above resolves
   `$WEB_SVC` explicitly instead of hardcoding it.

4. **Not stale, just worth adding:** CLAUDE.md's "BlackbirdBot" section has no
   mention of the star-topology requirement, the single `pitch` post type, the
   one-pitch-per-day cap, or hub auto-activation — all genuinely new
   operational facts about how this role behaves after this branch. Not a
   correctness bug (nothing currently there contradicts them), but a future
   reader debugging "why didn't my lab bot's second pitch of the day post" or
   "why does the hub reply to posts nobody tagged it in" has no CLAUDE.md
   pointer to the answer. Worth a short addition alongside the existing
   bullets there.
