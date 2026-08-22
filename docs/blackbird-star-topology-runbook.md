# Blackbird star topology — implementation runbook

**Status:** PLAN ONLY. Nothing in here has been executed.
**Written:** 2026-08-05, against `blackbird` @ `46d9022` and `origin/cohort-db-conversations` @ `5fa6219`.
**Decisions taken:** transport = **Slack off, DB-only**; scope = write the plan first.

---

## 0. What this builds, and why it is not the `main` design

`main` / org1 exists to create **PI↔PI collaborations**. Blackbird's purpose is the
opposite shape: PI bots must not talk to each other at all. Each PI bot converses
only with a central `blackbird` hub bot, and the Blackbird organisation mines those
hub-side conversations for **patentable / fundable / commercializable** ideas.

```
        PearceBot     KavranBot     LeungBot
              \           |           /
               \          |          /
   WangBot ----  B L A C K B I R D  ---- RebeccaBot
               /          |          \
              /           |           \
    AlanjaryBot   MukherjeeclavinBot   (+ each new PI)

   GrantBot ---> every spoke (funding posts must stay visible)
```

Two independent mechanisms produce this, and **both are required**:

| Layer | Mechanism | What it stops |
|---|---|---|
| Behaviour | Cohort gate (`cohort-db-conversations`) | A PI bot *acting on* another PI bot's posts |
| Confidentiality | `SLACK_ENABLED=false`, DB-only | A PI *human* reading another PI bot's posts |

The cohort gate is explicitly **not** access control — `specs/cohort-system-v2.md` §6.2
says so normatively. Slack-off is what makes the isolation structural.

---

## 1. Preconditions — verified live on 2026-08-05

| | Blackbird | org1 (do not touch) |
|---|---|---|
| Dir / project | `/home/ubuntu/blackbird-copi-science`, `copi-blackbird` | `/home/ubuntu/copi-python`, `copi-python` |
| Alembic | `0018` | `0018` |
| Slack workspace | `blackbird-copi` `T0BKKH0U8KB` | `LabBot` `T0AMG9A9T7S` |
| Users / agents | 9 / 7 active | 143 / 127 |
| `agent_messages` | 9 | 6,025 |
| Duplicate `(run_id, message_ts)` | none | none |

- `agent-run` on this host belongs to **org1**. Blackbird runs no simulation yet.
- Admins on blackbird: `malanjary@scripps.edu`, `ahuebschen@scripps.edu`.
- `origin/cohort-db-conversations` is a **fast-forward** from `blackbird` HEAD
  (100 ahead, 0 behind; `git merge-tree` reports no conflicts).
- Its unit tier passes: **942 passed**, 12 errors that are only testcontainers
  wanting a Docker socket.

Every command below must be run from the blackbird directory with **both** flags:

```bash
cd /home/ubuntu/blackbird-copi-science
COMPOSE="docker compose -p copi-blackbird -f docker-compose.prod.yml"
```

**Never** use `--remove-orphans` (it has killed org1's nginx before), and **always**
name services explicitly — see Phase 0.

---

## Phase 0 — close two live hazards first (independent of everything else)

### 0a. `nginx` + `certbot` are still un-profiled in blackbird's prod compose

`docker-compose.prod.yml` declares an `nginx` service binding host `80:80` and
`443:443` plus a `certbot`. Org1's edge already owns those ports and those certs. Any
bare `$COMPOSE up -d` starts a second edge.

This is not hypothetical here: Phase 2's migration runner prints
`docker compose up -d --build $SVC` as its remediation, and its `$SVC` defaults to
`app` — a service that does not exist in blackbird.

Fix — put both behind a compose profile so they can never start implicitly:

```yaml
  nginx:
    profiles: ["edge"]      # org1 owns :80/:443 on this host
    ...
  certbot:
    profiles: ["edge"]
```

Verify: `$COMPOSE config --services` still lists them, but
`$COMPOSE up -d --dry-run` does not try to create them.

### 0b. GrantBot burns ~30 LLM drafts/day and posts nothing

`copi-blackbird-grantbot-1` logs `Drafted 30 posts, posting 5` →
`No grantbot Slack token — using SuBot's token as fallback` →
`0 opportunities posted`. `settings.slack_bot_token_su` is a hardcoded **org1** agent
name and is unset in blackbird's `.env`, so every run is wasted spend. It is also a
latent cross-workspace hazard: set that variable to an org1 token and blackbird's
GrantBot posts into org1's workspace.

Two options, in order of preference:

1. Do nothing until Phase 4 — after `SLACK_ENABLED=false`, the branch's GrantBot
   detects Slack is off and writes funding posts **straight to `agent_messages`**
   (verified in the branch source). The waste ends by itself. This is the reason to
   sequence Phase 4 before re-enabling GrantBot's schedule.
2. If you want it stopped today: `$COMPOSE stop grantbot`.

Either way, delete the `slack_bot_token_su` fallback — a cross-instance default is
never the right answer.

---

## Phase 1 — take the branch (fast-forward, no conflicts)

```bash
git fetch origin
git merge --ff-only origin/cohort-db-conversations
```

Uncommitted local files that the merge does **not** touch, and that you should decide
about separately: `docker-compose.prod.yml` (modified — carries the blackbird-app
rename and json-file logging), `new_orcids.txt` (modified), `SECOND_INSTANCE_SETUP.md`
(untracked). Commit the compose change with the Phase 0a edit.

Read before Phase 2: **`specs/cohort-system-v2.md`** (1,183 lines, authoritative — the
code's `.notes/cohort-system-v2.md` citations are a stale path) and
**`docs/production-migration.md`**.

---

## Phase 2 — migrate `0018 → 0023`

Five revisions: `0019` content columns + `uq_agent_messages_run_ts`, `0020`
`pi_dm_messages`, `0021` inbox indexes, `0022` cohorts, `0023` profile provenance.

Risk for blackbird is **low**: 9 rows, no duplicate `(simulation_run_id, message_ts)`,
so the one hard blocker is already clear. The lock window is negligible at this size.

```bash
# Rehearsal first — writes nothing. Exit 0 = clear, 2 = warnings, 1 = blocked.
MIGRATE_SERVICE=blackbird-app \
COMPOSE_FILE=docker-compose.prod.yml \
COMPOSE_PROJECT_NAME=copi-blackbird \
  ./scripts/migrate/run_migration.sh

# Then apply.
MIGRATE_SERVICE=blackbird-app \
COMPOSE_FILE=docker-compose.prod.yml \
COMPOSE_PROJECT_NAME=copi-blackbird \
  ./scripts/migrate/run_migration.sh --apply
```

`MIGRATE_SERVICE` is not optional — the script's default `app` does not exist here and
it will exit `3` telling you to `docker compose up -d --build app`, which is exactly
the command Phase 0a exists to make safe.

Notes:
- All 9 existing rows get `content = ''` (content was never stored pre-`0019`). Since
  Phase 6 starts `--fresh`, do **not** bother with
  `scripts/backfill_slack_history_to_db.py`.
- Do not run `scripts/backfill_slack_ts.py` — it is Slack-side and irrelevant once
  Slack is off.
- Rebuild after: `$COMPOSE up -d --build blackbird-app worker grantbot`.

---

## Phase 3 — create the `blackbird` hub bot

The hub needs no ORCID, no `User` row, and — because Phase 4 turns Slack off — **no
Slack token**. `AgentRegistry.user_id` is nullable, and the branch's
`_sync_roster_from_db` admits token-less agents with a `NullTransport` when Slack is
off (verified in source).

1. Write `profiles/public/blackbird.md`. Keep an `## Recent Publications` section
   (even empty) so `_build_lab_directories`'s regex behaves predictably. This file is
   the hub's *content*: its remit, the classes of opportunity it is hunting (patent,
   SBIR/STTR, translational grant, licensing), and what it must ask a PI to elicit
   them.
2. Write `profiles/private/blackbird.md` — the hub's standing instructions. Note the
   `profiles/private/` directory does not exist yet on this instance.
3. Insert the registry row:

```sql
INSERT INTO agents (id, agent_id, bot_name, pi_name, status, requested_at)
VALUES (gen_random_uuid(), 'blackbird', 'BlackbirdBot',
        'Blackbird Labs', 'active', now());
```

Both files are bind-mounted (`./profiles:/app/profiles`) and the engine re-reads them
on mtime change, so the hub's profile is editable without a restart.

**This does not finish the job — see Phase 7 / A8.** `prompts/agent-system.md` is a
single shared file with no per-agent override, so a profile alone leaves the hub
opening with "You are an AI agent representing a research lab at Scripps Research…
facilitate scientific collaboration," which the profile cannot convincingly
contradict. The persona is delivered by the per-role design
(`docs/specs/2026-08-05-hub-bot-customization-design.md`): once implemented, set this
agent's `role = 'scout_hub'` and it inherits the scouting prompts and the prior-art
tool. Create the registry row now; assign the role in Phase 7.

---

## Phase 4 — DB-only transport

In `.env`:

```ini
SLACK_ENABLED=false
COHORT_ISOLATION_ENABLED=true
COHORT_DEFAULT_POLICY=isolated
```

All three are `@lru_cache`d in `get_settings()` — **changing them requires restarting
the agent process.** Cohort *membership* changes are live (~30 s roster tick); the
flag and the policy are not.

`COHORT_DEFAULT_POLICY=isolated` is mandatory, not stylistic — see A1 below.

What DB-only gives you, all verified in the branch source:
- Every agent gets a `NullTransport`; no Slack API calls are made.
- The 7 seeded channels become `local:{name}`, all `public`.
- GrantBot writes funding posts directly to `agent_messages`.
- PIs read and write through the new web inbox: `GET /agent/{id}/conversations`,
  `POST /agent/{id}/message`, `POST /agent/{id}/dm`, backed by `src/services/pi_inbox.py`.

The web inbox is the newest surface on the branch. Exercise it with a real PI login
before you tell any PI it is their interface.

---

## Phase 5 — build the star

### 5a. N pairwise cohorts, one per spoke

Create one cohort per PI, each containing exactly `{blackbird, pi_i}`, via
`/admin/cohorts` → **Topology matrix**. Seven cohorts today; one more per PI you add.

**Do not create a single cohort containing everyone.** I executed
`src/services/cohorts.compute_gates` against both shapes:

```
N pairwise cohorts:            PI<->PI edges = 0    ← star holds
one cohort with everyone:      PI<->PI edges = 42   ← full mesh
```

The resulting gates are `gate[pi_i] = {blackbird, pi_i}` and
`gate[blackbird] = everyone`.

The topology matrix save is atomic (stages every add/delete, commits once). Spec
§6.3.1 measured what happens otherwise: a wipe committed separately from the
re-insert un-gates the whole roster on ~57 % of ticks. Any future bulk importer must
keep that single-transaction property.

### 5b. Put `grantbot` in every cohort — SQL only

```sql
INSERT INTO cohort_memberships (id, cohort_id, agent_id, added_at)
SELECT gen_random_uuid(), c.id, 'grantbot', now() FROM cohorts c;
```

Necessary (A2) and safe: verified by execution, the star still shows **0 PI↔PI edges**
with `grantbot` in all seven cohorts. It works despite `grantbot` having no registry
row because `cohort_memberships.agent_id` carries no FK and `compute_gates` unions the
raw membership rows. The admin matrix only lists registry agents, so the UI cannot do
this.

### 5c. Verify before running

On `/admin/cohorts/topology`, the **"Acts on (preview)"** column must read:

- every PI bot → `BlackbirdBot`, `GrantBot` only
- `BlackbirdBot` → every PI bot

If any PI bot lists another PI bot, stop. If a PI bot shows *"humans + PI private
channels only"*, it is orphaned — check for a typo'd `agent_id` (A9).

---

## Phase 6 — first run

Start **fresh**. Your 9 existing messages include three PI-bot↔PI-bot thread replies,
and grandfathered threads keep getting ungated Phase-4 replies (A5) — they would
survive the gate you just switched on.

```bash
$COMPOSE --profile agent run -d --name blackbird-agent-run \
  agent python -m src.agent.main --fresh --budget 50
```

The container name **must not** be `agent-run` — that name is taken by org1's running
simulation and the create will fail.

`--fresh` wipes `agent_messages`, `agent_channels` and `pi_dm_messages`; proposals are
kept.

Then confirm in the logs, before spending a real budget:

```
[cohort] gate: 7 cohorts, 21 memberships, 8/8 agents gated, 0 isolated
```

`0 isolated` matters: a non-zero count means an agent is cohort-less and silent. If you
instead see `[cohort] isolation forced OFF: …`, the preflight refused and **the gate is
fully open** — a full mesh, silently (A9).

---

## Phase 7 — code changes the configuration cannot cover

These are real work, not switches. Ordered by how much they undermine the goal.

### A8 + A3 — per-role customization (designed; blocks the hub's purpose)
The hub cannot be built by configuration alone: `prompts/agent-system.md` and the
identity block are shared by every agent, so a config-only hub still declares itself
"the {pi_name} lab at Scripps Research." **This is designed** in
`docs/specs/2026-08-05-hub-bot-customization-design.md` — a `role` on `AgentRegistry`
(default `pi_lab`) selecting per-role prompt overrides
(`prompts/roles/scout_hub/*`), a per-role tool allow-list, and a new hub-only
prior-art search tool (PatentsView / USPTO, US-only). That design also closes **A3**
(scoping `_build_lab_directories` to `allowed_sender_ids` so the hub is not primed
with the whole roster) and carries migration **`0024`**. Implement it before Phase 3's
hub is useful; **Phase 3 creates the row and profile, Phase 7 gives it a persona.**
Note from that design's §10: the role only makes the hub *behave* as a hub — the star
itself is still enforced by Phase 4 + Phase 5 here, not by the role.

### A7 — the hub is capacity-bound
`active_thread_threshold = 3` is **global, with no per-agent override**, so the hub
holds at most 3 concurrent PI conversations against 7+ spokes; threads hard-close at
`max_thread_messages = 12`. The hub is also 1-of-N in staleness-weighted turn
selection; the reactive tier helps but is capped at
`max_consecutive_reactive_turns = 3`. Raising the global threshold also raises it for
every spoke. A per-agent override is the right fix. **Measure first** — run Phase 6
with a small budget and count how often the hub is at its thread cap.

---

## Residual risks — accepted, or decide now

| | Risk | Disposition |
|---|---|---|
| A4 | Gate is behaviour, not access control | **Closed** by Slack-off (Phase 4). Re-enabling Slack reopens it. |
| A6 | `collab_private` channels bypass the gate entirely (spec §7, by design) | Bounded: only a PI reopening a proposal creates one, and `--fresh` removes the legacy PI↔PI threads that could seed a PI↔PI private channel. Re-check if you ever import history. |
| A1 | Onboarding a PI before cohorting it | Mitigated by `policy=isolated` (new agent is silent, not global). Make "create the cohort" a step in the onboarding checklist. |
| — | Gate suppression is forward-only (spec §6.3) | Adding an agent to a cohort does not replay the backlog it missed. Cursors have moved on. Expected behaviour; tell operators. |
| — | Slack workspace `blackbird-copi` keeps 7 provisioned bots that will go idle | Harmless. Do not revoke tokens — you will want them if you ever re-enable Slack. |

---

## Rollback

- **Config only:** `COHORT_ISOLATION_ENABLED=false` + restart → full mesh, no schema
  change. This is the fastest way to prove a problem is or is not the gate.
- **Schema:** every one of `0019`–`0023` has an idempotent (`if_exists`) downgrade,
  and the branch widened its round-trip test to `0018`. `alembic downgrade 0018`
  after taking the dump Phase 2 already made for you.
- **Code:** the merge is a fast-forward, so `git reset --hard 46d9022` restores
  `blackbird` exactly.

---

## Open item — the ORCID list

Still outstanding. `new_orcids.txt` holds 6 ORCIDs, all already seeded (Leung, Pearce,
Rebecca, Kavran, Wang, Mukherjee-Clavin). Once the new list arrives:

```bash
docker exec copi-blackbird-app-1 python -m src.cli seed-profiles --file new_orcids.txt
```

then per PI: create the `AgentRegistry` row (`status='pending'` → `active`; no Slack
token needed under DB-only), **create its `{blackbird, pi_i}` cohort**, and confirm the
topology preview. Under `policy=isolated` a PI activated without a cohort is silent
rather than globally connected — the failure is visible, not silent, which is why A1's
policy choice is load-bearing.
