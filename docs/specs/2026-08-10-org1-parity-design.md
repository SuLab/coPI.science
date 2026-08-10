# org1 parity: porting the generic blackbird work to copi.science

**Date:** 2026-08-10
**Status:** Design, approved. Not implemented.
**Branch:** `org1-parity` (off `origin/cohort-db-conversations` @ `5fa6219`)
**Destination:** `main`, via `cohort-db-conversations`

## 1. The problem

Five branches carry production-relevant work, and no two of them agree:

| Branch | Head | Relationship |
|---|---|---|
| `main` | `b7edcbc` | ancestor of `cohort-db-conversations` |
| `cohort-db-conversations` | `5fa6219` | `main` + 77; **strict ancestor of `blackbird`** |
| `blackbird` | `22dd952` | `cohort-db-conversations` + 119 |
| `copi-prod` | `0c7c4be` | **what org1 actually runs.** Not an ancestor of anything above: 4 unique commits, alembic head `0018` |
| `blackbird-main` | `ac5a2c9` | deprecated |

Because `cohort-db-conversations` is a strict ancestor of `blackbird`
(`git merge-base` returns cohort's own head; `rev-list --left-right` is `119 / 0`),
reconciliation is **subtraction, not merge**: decide which of 119 commits belong on
org1. `copi-prod` is the exception — it is genuinely divergent and must be merged in,
or a deploy silently reverts production.

### 1.1 The two instances

| | **org1** | **blackbird** |
|---|---|---|
| Path / compose project | `/home/ubuntu/copi-python`, `copi-python` | `/home/ubuntu/blackbird-copi-science`, `copi-blackbird` |
| Domain | `copi.science` | `blackbird.copi.science` (proxied **by org1's nginx**) |
| Web service | `app` | `blackbird-app` (an *uncommitted* host-local compose edit; the tracked `docker-compose.prod.yml` says `app` on every branch) |
| Roster | Scripps Research labs | 56 agents, 57 of 60 profiles Johns Hopkins |
| Topology | **mesh** — peer-to-peer lab collaboration | **star** — 56 cohorts of `{pi, blackbird, grantbot}` |
| Cohort gate | `cohort_isolation_enabled=False` | `True`, `cohort_default_policy="isolated"` |
| Product | cross-lab collaboration proposals | BlackbirdBot screens PI ideas for patentability / fundability / commercialisability |
| alembic | **0018** | 0025 |

Sources: `CLAUDE.md:56-79` (blackbird), `docs/specs/2026-08-06-role-topology-post-type-gating-design.md:9-15,69,145`,
`specs/cohort-system-v2.md:390-405`, `src/config.py:347-370`, commit `0e1ac52`'s message,
`copi-prod:476f46b`.

The divergence is topological, and the design doc for post-type gating states it
plainly: *"`pi_lab` in org1's mesh **should** make cross-lab idea posts — that is the
product. The same role in this star must not."*

## 2. Decisions

1. **Scope: generic parity, minus the Blackbird product.** Everything that is not the
   scouting product: cohort feed + topology fixes, security/500 fixes, the lint +
   hermeticity repair, the rate limiter, role infrastructure, post-type modules.
2. **org1's cohort gate is off now, but cohorts are coming.** So the feed gating lands
   *before* the flip, and post-type enforcement waits *for* it.
3. **`copi-prod` is merged in first**, establishing the invariant that the branch is
   never behind production.
4. **org1's prompts stay frozen.** No base-prompt commit is ported. The post-type
   machinery lands inert; enforcement is deferred.

## 3. Approach

Ordered cherry-pick in blackbird chronological order, with surgical commits
hand-applied. Chosen over (a) merging blackbird whole and reverting the product —
which would put migration 0025 on org1's branch and require restoring the `.ambr`
snapshot — and (b) feature-squashing to end state, which discards commit messages
that in this repo carry the measured evidence ("146 of 146 tagged posts addressed an
unreachable agent", "265 → 257 findings", "took the hub off the air for 161 turns").

Measured conflict cost of ordered replay: **9 commits / 13 files** of 54. Every
hand-applied commit carries a `Ported-from: <sha> (partial)` trailer naming what was
dropped, so the original reasoning stays findable on `origin/blackbird`.

## 4. The port set

**54 commits ported in full, 8 in part, 57 excluded entirely. Branch: 64 commits.**

Ported in part: `9714f26`, `6b76f27`, `29fc8f1`, `f32a83e`, `e116feb`, `1b44e1c`,
`0a57e41`, `10d598f`. Each contributes one or more generic hunks to a hand-applied
commit; the rest of each is dropped.

### Phase 0 — `git merge origin/copi-prod` (1 commit)

Verified by `git merge-tree`: four conflicts, **zero lines of code**.

| Conflict | Resolution |
|---|---|
| `.gitignore` | union — cohort's `docs/superpowers/` plus copi-prod's `.claude/`, `logs/`, `scripts/_*`, `*.bak.*` |
| `scripts/audit_pub_dois.py` | take both: copi-prod redacted a **real ORCID** (`0000-0002-9943-7557` → placeholder) in the usage block; cohort changed the invocation to `docker compose exec app`. The redaction is a privacy fix in a public repo and must not be lost. |
| `scripts/backfill_agents.py` | take both (copi-prod moved seed files under `data/cohorts/`; cohort changed the invocation) |
| `scripts/generate_sparsedata_user.py` | take both, same shape |

`src/config.py` auto-merges, so `audit_recipients` / `audit_recipient_list` arrive
free. `.dockerignore`, the `copi-edge` external network, the `blackbird.copi.science`
vhost, and 9 maintenance scripts arrive as fast-forward adds.

**What this prevents.** Deploying cohort as-is to org1 would have: deleted the tracked
`.dockerignore`, so `Dockerfile`'s `COPY . .` resumes **baking `.env` secrets into
image layers**; reverted the `copi-edge` network and vhost, taking
**blackbird.copi.science dark** (org1's nginx is the edge for both instances);
reverted `audit_recipients` to hardcoded addresses; and deleted 9 operator scripts.

**Post-merge assertion.** `git diff org1-parity origin/copi-prod` must show only
removals cohort made deliberately (the dead onboarding templates from `d1005b1`, the
pre-0019 alembic files). Anything else is a hidden prod revert.

### Phase 1 — Buy lint headroom (1 hand-authored commit)

`cohort` sits at **260 `src/` ruff findings against `ci.sh`'s 260 ceiling** — zero
headroom, so the first feature commit breaches it.

- Cherry-pick verbatim from `3a23e73`: `src/agent/message_log.py`,
  `src/dependencies.py`, `tests/integration/test_proposal_review.py`,
  `tests/unit/test_email_templates.py`, `tests/unit/test_slack_tokens.py` — all five
  blob-identical to `3a23e73^`.
- Hand-apply three one-line import removals: `typing.Any` and `PostRef` from
  `src/agent/agent.py`, `sys` from `src/agent/main.py`. (These conflict as patches
  because blackbird's parent has the role imports, but the removals themselves apply —
  cohort's per-file counts of 3 and 4 match `3a23e73^` exactly.)

Effect: `src/` 260 → ~253. `Ported-from: 3a23e73 (partial)`.

### Phase 2 — Role infrastructure (9 picks + 2 hand-applied)

```
a655ede  feat(roles): prompt-path resolution with per-role fallback
46a8391  feat(roles): role.toml manifest with tool allow-list and safe fallbacks
ac2da9e  refactor(agent): role-aware prompt loading; collapse 3 builders into 1
48e4d05  feat(db): add agents.role column (migration 0024)
ffef698  fix(migrate): advance migration tooling's target from 0023 to 0024
da0625d  feat(roster): thread role through roster reads; pick up role changes live
711b13b  feat(tools): per-role tool allow-list, enforced in Phase 4 and the executor
bc293d9  fix(cohort): scope lab directory to the cohort gate (runbook A3)
4ec8ab7  feat(admin): view and set agent role; show role on topology page
```

Verified no-ops for `pi_lab`: `resolve_prompt_path` falls through to
`prompts/{file}`; `roles.DEFAULT_TOOLS` is **exactly** cohort's four
`TOOL_DEFINITIONS` entries; `ac2da9e`'s `prompts/identity.md` is byte-identical to the
three duplicated literals, **"at Scripps Research" included**, and moved zero
snapshots. `711b13b` lands before `5367027` in blackbird order, so it applies clean and
yields `tools_for_role` with only the four base tools.

**Hand-applied A — `6b76f27` (partial), 2 lines.** `build_phase5_prompt` loaded
`PROMPTS_DIR / "phase5-new-post.md"` directly instead of `_load_prompt()`. Without
this the role mechanism is internally inconsistent: agent-system, identity, phase-2,
phase-2-prune and phase-4 honour role overrides; phase 5 silently does not. pi_lab's
phase-5 snapshot is unchanged (stated in the commit). Drop everything else in
`6b76f27` — it is the scout_hub prompt tree.

**Hand-applied B — migration-tooling completion.** Four edits `ffef698` does not carry:

1. `tests/integration/test_harness_smoke.py`: head pin `0023` → `0024`
   (`Ported-from: 9714f26 (partial)` — the rest of that commit is the live
   PatentsView test).
2. `scripts/migrate/preflight.py`: add `PlannedObject("0024", "column", "role", "agents")`.
   The entry lives in `517a564` (excluded), so without it `REVISION_ORDER` reaches
   0024 while preflight's collision check silently skips `agents.role`.
3. `tests/unit/test_migration_checks.py`: extend the drift-guard tuple from
   `("0019"…"0023")` to include `"0024"`.
4. `scripts/migrate/postflight.py`: add
   `("agents", "role", "character varying", False, "'pi_lab'::character varying")` to
   `EXPECTED_COLUMNS`, so 0024 is *verified* after the window rather than assumed.
   Blackbird documented this gap (`VERIFIED_REVISIONS`); closing it is cheap here
   because 0024 creates no table, so `CHAIN_CREATED_TABLES` is unaffected.

Also reword `ffef698`'s comment: it justifies adding `0023` to
`SUPPORTED_START_REVISIONS` as "production's current stamp", true for blackbird and
false for org1 (0018).

### Phase 3 — Cohort feed and topology (18 picks)

```
8bc0e24  docs: design for cohort-scoped conversations feed, threads, topology payload
d84ce6b  docs: implementation plan for cohort-scoped feed, threads, topology payload
0efd6a5  fix(admin): topology matrix payload 3,360 fields -> 116
d2b3b21  fix(admin): bound the topology cross product by table size, not payload size
ffad1a1  feat(feed): gate_clause — the cohort gate as a SQL predicate
942b31b  feat(feed): resolve_agent_gate via the engine's compute_gates
5bf587e  docs(feed): clarify resolve_agent_gate docstrings post-review
230a4c0  fix(feed): cohort-scope the conversations page and select thread roots
6d94148  fix(feed): own-post carve-out, gate the reply count, and pin the regressions
f77a0a2  feat(feed): thread expand endpoint returning a gated replies partial
ddb3892  fix(feed): prove replies are actually gated; dedupe channel-set computation
0c04be6  feat(feed): render roots with a reply badge and expand-on-click
0dd94db  fix(feed): guard the thread-expand link against a double-click mid-fetch
44f1ad0  fix(feed): cover the plural badge and href correctness gaps from review
cc3c90f  docs(cohort): correct spec/docstrings now that the PI feed is gated
4bc5cbe  fix(feed): scope reply queries to the root's channel; log preflight fail-open
a968d7a  test(admin): pin the topology marker/cell cross-product invariant
7f6b304  docs(cohort): fix the amendment pointer to specs/, not .notes/
```

Two of these are live defects on org1 today, independent of the gate:

- **The admin topology matrix cannot be saved.** 60×56 posts 3,528 form fields against
  Starlette's `max_fields=1000`. `d2b3b21` additionally bounds a multiplicative
  cross-product DoS (25k ids per side = 50k fields, under the cap, a 625M-entry set).
- **The PI conversations feed had no content filter** beyond channel name. Inert while
  the gate is off (`gate_clause(None)` returns `true()`), but it is the fix that makes
  the gate flip safe, and `specs/cohort-system-v2.md` §6.2 is amended to record the
  deliberate narrowing of "the gate is not access control".

`0efd6a5` touches `templates/admin/cohort_topology.html`, which also carries the Role
column from `4ec8ab7` — Phase 2 lands first, so it applies clean.

### Phase 4 — Scheduler and rate limiter (15 picks)

```
15a277e  docs(spec): load-proportional budget and scheduling for star topologies
7c6768e  docs(plan): implementation plan for load-proportional budget/scheduling
5654271  docs(plan): adversarial audit against HEAD 7f6b304 — fix 4 defects
09b83aa  feat(sched): _agent_load — the shared load signal
9932645  feat(config): rate-limiter settings + optional per-role allowance
0929870  feat(sched): call ledger — record_api_call maintains both counters
6d1deed  fix(roster): adopt a Slack client when a live agent gains a token
0821372  feat(sched): sliding-window rate limiter replaces the cumulative cap
92e4989  feat(sched): rebuild call_times from llm_call_logs within the window
f5531d2  fix(sched): make step 4b idempotent and DB-test the window query
e111732  feat(sched): load-proportional selection weight and reactive tiebreak
bc4dd3a  feat(cli): deprecate --budget, default it off, document the replacement
00e174f  test(sched): production regression for the run-4f1e8395 hub bench
3a23e73  fix(ci): make settings-dependent tests hermetic; clear lint debt
46d3a61  fix(sched): a throttled roster must back off, not end the run
```

Ported as one unit — `Agent.record_api_call` is the single write point for both
counters, and the `pi_handler.py` / `llm.py` `on_retry` hooks are what keep the live
and rebuilt ledgers consistent. The bug it fixes is generic and severe: `_rebuild_state`
restores `api_call_count` from `llm_call_logs`, so a crossed cumulative cap benches an
agent **permanently, across restarts**.

Conflict resolutions in this phase:

- `9932645` / `src/config.py` (blocked by `0621ef3`): keep only
  `llm_rate_window_seconds`, `llm_calls_per_load_per_window` and
  `_guard_rate_limiter_settings`. Drop the `uspto_api_key` / `patentsview_api_key`
  block.
- `9932645` / `tests/unit/test_roles.py` (blocked by `6b76f27`): see the
  `test_roles.py` trim at the end of this phase.
- `0929870` / `src/agent/agent.py` (blocked by `6b76f27`): take only the
  `record_api_call` hunk.
- `bc4dd3a` / `CLAUDE.md` (blocked by `996cca7`): drop the hunk. org1's CLAUDE.md is
  authored separately (§8).
- `3a23e73` / `tests/unit/test_patents.py` (blocked by `4322e5c`): drop the hunk.
  Its `src/agent/agent.py` and `src/agent/main.py` hunks are already in Phase 1; take
  the remaining test hermeticity fixes for `test_agent_page.py`, `test_cohort_admin.py`
  and `test_roles.py`.

**`test_roles.py` — nothing is deleted; the scout_hub tests never arrive.** Blackbird's
copy ends at 27 tests, of which 10 read the real `prompts/roles/scout_hub/` tree. Those
10 are *introduced* by excluded commits (`6b76f27`, `2bd0289`, `9d5a1d7`, `3f1f91d`,
`eadde02`, `988eac1`, `5114905`, `f7a9f68`), so on this branch they are never written in
the first place. Keeping them would have dragged `patents.py`, `blackbird_rubric.py` and
`specialists.py` along — they assert `"search_prior_art" in spec.tools`, quote the
Blackbird rubric, and check the Baltimore gating criterion.

What that means operationally is *conflict resolution, not deletion*. Three ported
commits patch this file and each needs a specific decision:

- `9932645` adds five `tmp_path`-based rate-override tests. **Keep all five.** The
  conflict is purely positional — they append after scout_hub tests that do not exist.
- `dc371af` adds six tests. **Keep the four `tmp_path`-based `post_types` tests; drop
  `test_scout_hub_declares_its_two_post_types` and
  `test_scout_hub_cannot_post_a_cross_lab_idea`**, which call
  `load_role("scout_hub")` and need the `role.toml` this branch does not ship.
- `3a23e73`'s hunk edits the `from pathlib import Path` and `_load_role_real` import
  lines. **Drop the hunk entirely** — both imports exist only to serve the scout_hub
  tests, so neither is present here and there is nothing to lint.

Final content: the mechanism tests from `a655ede` and `46a8391`, five rate-override
tests from `9932645`, four `post_types` tests from `dc371af`. Every one uses `tmp_path`
and a monkeypatched roles directory; none touches `prompts/`.

### Phase 5 — Role prompt completion and generic fixes (7 picks + 3 hand-applied)

```
2467229  fix(agent): phases 2 and 4 must honour role prompt overrides
bc40d20  fix(agent): phase2-prune must also honour role prompt overrides
683c09a  feat(scout_hub): drive the interview off the screening rubric  [thread_guidance extraction]
44f09be  fix(llm): detect and log a still-truncated retry; let callers count it
21869e2  fix(sched): suppress a post that strips to nothing instead of ghost-posting it
73a78c3  fix(admin): a Slack post with no mappable sender must not 500 /admin/discussions
5fb68c0  fix(admin,public): close the null-agent_id 500 class, an unauthenticated vote-tamper hole
```

`683c09a` extracts `thread_guidance.py`; its `_PI_LAB` strings are byte-identical to
the pre-refactor `agent.py` literals and are pinned by the snapshot. Its `_SCOUT_HUB`
dict is dead code without the role, and is kept rather than trimmed so the file stays
mergeable with blackbird.

`5fb68c0` closes an **unauthenticated vote-tamper hole**: `if vote_obj.voter_token and
token and ...` meant omitting `voter_token` set `token = None` and bypassed the
ownership check entirely.

Conflict resolutions:

- `73a78c3` / `src/routers/admin.py` (blocked by `f32a83e`): hand-apply the
  `available_agents` None-guard.
- `5fb68c0` / `src/agent/simulation.py` (blocked by `10d598f`) and
  `tests/integration/test_opportunity_assessment_persistence.py` (blocked by
  `66948dc`): **drop both.** The `simulation.py` hunk is the assessment-persist fix;
  the test file is Blackbird product. Keep `public.py`, `admin.py`, the two templates,
  and the two characterization tests.
- `21869e2` / `src/agent/simulation.py` and `tests/unit/test_simulation_logic.py`
  (blocked by `f4488f7`, `1462d29`): hand-apply the empty-post suppression onto
  cohort's `_post_message`. Three adjustments, none of them mechanical:

  1. **The return contract must come with it, as `-> bool`.** `21869e2` alone writes a
     bare `return`, which is useless to `e116feb`'s caller guards below. On blackbird
     the contract arrives in `29fc8f1` (`-> None` → `-> bool`, `return False` at both
     bail-outs) and is then widened to `-> str | None` by `1b44e1c` — but that widening
     exists *only* so an `opportunity_assessments` row can store the post's `slack_ts`,
     which org1 has no use for. Take `29fc8f1`'s `-> bool` and stop there.
     `Ported-from: 21869e2, 29fc8f1 (partial)`.
  2. **Reword the comment.** `21869e2`'s hunk sits directly below
     `text = _strip_assessment_sidecar(text)` and its comment is written around that
     call, which does not exist on this branch. The guard is still correct — a truncated
     response can strip to empty through the `</?slack_message>` substitution alone —
     but the rationale must be restated in those terms rather than inherited.
  3. Insert after `text = re.sub(r"</?slack_message>", "", text).strip()`, which is the
     line the hunk actually anchors to once the sidecar call is gone.

**Hand-applied C — `f32a83e` (partial).** `generate_with_tools` gains `on_retry`, both
of its retry sites re-check `stop_reason`, and `simulation.py`'s phase-4 call site
passes `on_retry=agent.record_api_call`. `generate_with_tools` is **the function
phase-4 thread replies use** — org1's entire product — and `44f09be` fixes only
`generate_agent_response`. Without this, one retry site swallows truncation silently
and the limiter undercounts every retried phase-4 turn. Drop B1/B2/B3 (`admin.py`
triage scoping, `assessments.html`).

**Hand-applied D — `e116feb` (partial).** The three `_post_message` caller guards:
the phase-4 reply site, the phase-5 private-channel flat follow-up, and the phase-5
thread-creating reply. `21869e2` makes `_post_message` return falsy; this is what makes
the callers *check* it. Without it, half the callers count the turn, clear
pending-reply/backoff state, and move posts into `active_threads` for a message nobody
saw. Drop the `_extract_assessment_json` rework and the assessment logging triage.

**Hand-applied E — `1b44e1c` (partial), 12 lines.** `action = action_data.get("action")`
plus a return when falsy. Cohort defaults a missing `action` to `"new_post"` and posts
anyway. **Drop the `max_tokens` 1000 → 2500 change in the same commit** (§7).

### Phase 6 — Post-type machinery, inert (5 picks + 2 hand-applied)

```
3fd8a91  fix(cohort): build the lab directory after the gate, not before
f231bc8  feat(post_types): the canonical vocabulary and the role+topology filter
20065e1  feat(post_types): add legacy idea->idea_crosslab alias resolution
dc371af  feat(roles): parse a post_types allow-list from role.toml
f2cbfe9  feat(agent): substitute {post_type_menu} in the phase-5 prompt
```

**No enforcement call.** `66948dc` is excluded (§7). The machinery is inert on org1:
`f2cbfe9` substitutes via `str.replace`, and org1's `prompts/phase5-new-post.md` has
no `{post_type_menu}` token, so nothing renders and nothing is judged.

`3fd8a91` is a no-op on a mesh (the directory filter only bites with the gate on) and
is kept as future-proofing for the flip.

Conflict resolutions:

- `3fd8a91` / `src/agent/simulation.py` (blocked by `f32a83e`): hand-apply the
  directory-after-gate reordering.
- `dc371af` / `prompts/roles/scout_hub/role.toml` (blocked by `6b76f27`): drop — the
  file is not on this branch. `dc371af` / `tests/unit/test_roles.py` (blocked by
  `f7a9f68`): per the Phase 4 trim.

**Hand-applied F — `0a57e41` (partial).** `parse_post_types` dedupes by name (dict,
last-wins, first-occurrence order preserved) with a WARNING. A real parse bug in code
we are porting: duplicate `[[post_types]]` entries produced two contradictory entries
while lookup kept only the last. The `render_menu` wording hunk in the same commit is
inert here (no menu is rendered) but is taken for file fidelity. **Drop the
`simulation.py` body-mention rejection and skip-backoff hunks** — both exist only to
serve enforcement.

**Hand-applied G — `10d598f` (partial).** `_recompute_allowed_sender_ids` refreshes lab
directories **even when the membership query raises**, so a stale-but-correct gate does
not leave a directory absent rather than merely stale. Pairs with `3fd8a91`, which must
land first. Plus two comment corrections in `post_types.py`. **Drop** the
`_normalize_tagged_agent` work, the `_post_type_rejections` counter, the
`_cohort_gate_banner.html` hunk, and every prompt hunk.

### Phase 7 — Post-type design doc (1 hand-authored commit)

Land `docs/specs/2026-08-06-role-topology-post-type-gating-design.md` at its final
state — the content of `d6bf5d7` as amended by `a187a1d`, `31cb20c` and `454fa86` —
so `post_types.py`'s docstring citation resolves. **Without**
`docs/specs/2026-08-06-post-type-gating-prompts-draft/`, which is blackbird's prompt
tree including scout_hub.

## 5. Migration and deploy

**Target: `0018 → 0024` in one window.** The runbook already exists on this branch
(`docs/production-migration.md`, 596 lines) and supports 0018 as a start revision;
its title and target move `0023` → `0024` and 0024 is appended to its §1 step table.

`0018 → 0019` is the expensive step: 7 columns, 4 indexes and
`uq_agent_messages_run_ts` on `agent_messages` under `ACCESS EXCLUSIVE`. Treat it as a
**full outage on that table** — a queued `ACCESS EXCLUSIVE` request blocks arriving
readers, not just writers. It hard-fails if duplicate `(simulation_run_id, message_ts)`
rows exist.

1. Read-only measurement (runbook §2, Q1–Q4): table size, duplicate groups, blocking
   sessions. Sizes the window before anything is touched.
2. `scripts/migrate/remediate_duplicates.py` dry run (executes in a `READ ONLY`
   transaction), then `--apply` if needed.
3. Rehearse: `scripts/migrate/preflight.py`. Writes nothing.
4. Apply: `COMPOSE_FILE=docker-compose.prod.yml ./scripts/migrate/run_migration.sh --apply`.
   Takes its own `pg_dump -Fc` and verifies the TOC first.
5. `scripts/migrate/postflight.py` against the step-1 snapshot.

`run_migration.sh` defaults `SVC=app`, which is correct for org1 (unlike blackbird).
But it shells out to **bare `docker compose`**, which resolves `docker-compose.yml` —
the dev stack. `COMPOSE_FILE=docker-compose.prod.yml` is not optional.

**Order: migrate → rebuild → restart.** Nothing migrates automatically: the prod web
command is a bare `uvicorn` and there is no `create_all`. New code against 0018 fails
at roster load, because `src/agent/main.py` selects `AgentRegistry.role`. The `agent`
service bakes `src/` into its image, so it needs `--profile agent build agent`, not
just `up -d --build app worker`.

**Rollback, and when the door closes.** `0024`'s downgrade is `if_exists`-guarded and
safe. `0019`'s is not: it runs
`alter_column("agent_messages", "agent_id", nullable=False)`. Immediately after the
window there are no NULLs, so a downgrade works — but as soon as the new code runs,
`_rebuild_state_from_slack` writes `is_bot=True, agent_id=NULL` for any Slack sender it
cannot map to a known bot (7 such rows measured on blackbird). **The moment the first
one lands, `alembic downgrade` past 0019 stops being an option and the only rollback is
restoring the step-4 dump.** Decide in advance how long that door is held open.

## 6. Verification

`./scripts/ci.sh` is the whole gate: alembic single-head and no duplicate ids →
upgrade→downgrade→upgrade round trip on a throwaway Postgres → ruff on `tests/` at zero
→ ruff ratchet on `src/` at ≤260 → full pytest with branch coverage ≥60.

**The decisive check — snapshot invariance.** The branch's central premise is that
org1's agent behaviour does not change. That reduces to one assertion:

```
git diff origin/cohort-db-conversations -- tests/characterization/__snapshots__/   # must be EMPTY
```

The only three commits in blackbird's 119 that touch `test_agent_turn_gm.ambr` are
`0e1ac52`, `0a57e41` and `10d598f` — all excluded, the latter two only in part, and
neither part touches the snapshot. A non-empty diff at any checkpoint means a prompt
changed and the port is wrong. **Never run `pytest --snapshot-update` to reconcile it.**

**The lint ratchet.** cohort is at 260 findings against a ceiling of 260. Phase 1 takes
it to ~253; the feature work adds ~+5 (`agent_page.py` +2, `admin.py` +2,
`conversation_feed.py` +1); projected final ~258. Blackbird's own head is 259
*including* the assessments route and four excluded modules, so ~258 is consistent from
both directions. **Measured, not assumed, at every checkpoint.** If it lands over 260
the fix is paying down debt in the files we touched — `SRC_LINT_MAX` is not raised.

**One test must be inverted.** `tests/unit/test_agent_prompts.py:17` asserts
`'Scripps Research' not in prompt`. Our `prompts/identity.md` keeps "at Scripps
Research", so it fails. Invert to `assert 'Scripps Research' in prompt` — turning
blackbird's Johns-Hopkins-driven assertion into a guard that a future port cannot
silently de-institutionalise org1's prompts. Line 16 (`'the Andrew Su lab'`) passes
either way.

**Checkpoints.** `ci.sh` takes ~6 minutes, so it runs after Phase 1 (proves headroom
bought), after Phase 3 (largest surface), after Phase 4 (largest behavioural risk), and
at the end. Between checkpoints: `ruff check src --quiet | wc -l`, the snapshot diff,
and `alembic heads`.

**What the round trip cannot prove.** With 0024 in the chain the round trip runs
`upgrade head → downgrade 0018 → upgrade head` against an **empty** throwaway database.
It will pass, and it cannot catch 0019's `agent_id → NOT NULL` downgrade failure, which
only occurs when rows exist. §5's rollback note is the mitigation.

**Coverage.** `COV_MIN=60`. Each feature is ported *with* its tests
(`test_conversation_feed.py` 926 lines, `test_hub_budget_scheduler.py` 730,
`test_post_types.py` 420, `test_roles.py` minus the 10 scout_hub tests,
`test_llm_service.py`, `test_thread_guidance.py`, `test_tool_gating.py`,
`test_lab_directory_ordering.py`, `test_state_rebuild.py`), so coverage should rise.
Porting `src/` without its tests is the one way this floor breaks.

**Hermeticity.** `3a23e73`'s test fixes matter only on a host with a provisioned `.env`:
7 tests read `SLACK_ENABLED`, `COHORT_ISOLATION_ENABLED` and
`OUTBOUND_EMAIL_ALLOWLIST` from it. The development checkout sets none of the four, so
the gate is green there today; a prod host would see those 7 fail before the port
begins.

## 7. Exclusions

**57 commits excluded entirely.** Every hash below is dropped in full; the 8 partials
(§4) are deliberately absent from this table.

| Group | Count | Commits |
|---|---|---|
| Patents / USPTO prior-art | 13 | `ce30c5f 27e88cd 5367027 5deac1e 0621ef3 4322e5c 9d4afc9 f32b7fe 678dfd1 1868089 b034e31 506d763 517a564` |
| Blackbird rubric, `opportunity_assessments`, triage UI | 12 | `3b59bd3 fcb6e7b c6943d4 53d4410 e91e6c5 1462d29 792f153 f4488f7 3919acc 00d5ebd a247ed8 265cd48` |
| Nine-evaluator specialist panel | 7 | `ebe03b0 3f3b992 a64b0ff d99656b ccd6f22 2e68d64 c9298fb` |
| scout_hub prompt content | 9 | `61dc019 2bd0289 9d5a1d7 3f1f91d eadde02 988eac1 5114905 f7a9f68 2af98de` |
| Base prompts — frozen | 3 | `0e1ac52 6fa3980 22dd952` |
| CLAUDE.md / instance runbooks | 3 | `996cca7 52f9e9a 805b6bd` |
| Blackbird design and plan docs | 8 | `6b7e7e7 87e1670 1f30556 663ea33 d6bf5d7 a187a1d 31cb20c 454fa86` |
| Deliberate deferrals | 2 | `66948dc 96c6243` |

`6b76f27` and `29fc8f1` are *not* listed here — each contributes one generic hunk (the
phase-5 `_load_prompt` fix; the `-> bool` return contract) and is otherwise dropped.
`54 + 8 + 57 = 119`.

`0e1ac52` is the one never to take: it strips "at Scripps Research" from
`agent-system.md`, `identity.md` and `_DEFAULT_IDENTITY` because, in its own words,
"57 of 60 public profiles say Johns Hopkins". Correct for blackbird, wrong for org1.

`d6bf5d7` and `454fa86` also carry
`docs/specs/2026-08-06-post-type-gating-prompts-draft/`, i.e. blackbird's full prompt
tree including scout_hub. Phase 7 lands the design doc only.

### 7.1 The two deliberate deferrals

**`66948dc` — post-type enforcement.** Layers 2 and 3 are provably dormant with the
gate off (`simulation.py` returns early when `allowed_sender_ids is None`), but
**layer 1 is not**: a model that omits or invents a `post_type` would publish nothing
where it publishes something today, against a prompt that never states a vocabulary is
enforced. org1's current enum
(`introduction|paper|help_wanted|idea|idea_crosslab|funding_collab`) is fully covered by
`DEFAULT_POST_TYPES` plus the `idea` alias, so the risk is small — but the benefit in a
mesh is near zero, because the artifact enforcement exists to prevent (259 `:bulb:`
posts, 0.8% reply rate, 146/146 tagged posts unreachable) is a star-topology pathology.
Enable it when cohorts flip on, together with a purpose-built org1 prompt variant, as
its own change with its own before/after measurement.

**`96c6243` — terminal-artifact backpressure.** Its `nothing_postable` condition
removes a cost-saving early return **for every role**. A blocked mesh `pi_lab` agent
always has `funding_collab` nominally available, so the early return stops firing and
the agent burns an LLM call to reach a phase-5 turn it then skips for lack of an FOA.
Pure cost on org1; the hub was the only beneficiary. Also drops `TERMINAL_POST_TYPES`,
which nothing else on this branch references.

### 7.2 Dropped from inside a ported commit

`1b44e1c`'s `max_tokens` 1000 → 2500 on the phase-5 call is **unconditional across all
roles**, sized for scout_hub's 11-section assessment artifact plus its JSON sidecar. On
org1 it is a 2.5× output-token ceiling increase with nothing to spend it on. Phase 5
takes only that commit's `action` guard.

## 8. Out of scope, recorded

- **org1's `CLAUDE.md` is authored separately, not ported.** Blackbird's version
  documents `blackbird-app`, `blackbird-agent-run`, the `copi-edge` two-stack warning
  and a `docker-compose.prod.yml` service name that **does not exist in the tracked
  file on any branch**. Its *Testing* section, however, is a doc-accuracy fix that
  applies to both instances: cohort's `CLAUDE.md` still describes the in-container
  pytest path and omits the round trip and `src/` ratchet that cohort's own `cc8490f`
  added. Salvage that section; discard every service name.
- **`coPI-podcast`** carries 66 unmerged commits (podcast/TTS, PI proposal evaluations,
  focus-agent mode). Untouched here; it is a separate reconciliation.
- **`blackbird-main`** is deprecated and has one unique merge commit.

### 8.1 A dangling citation, recorded rather than fixed

`docs/specs/2026-08-05-hub-bot-customization-design.md` is cited by `src/agent/roles.py`'s
module docstring, by migration `0024`'s docstring, and by
`docs/specs/2026-08-06-role-topology-post-type-gating-design.md` (which quotes it at
`:261` for the claim that runbook gap A3 was "recorded as closed"). **It exists on no
branch and was never committed.** The role mechanism's actual design is `roles.py`'s
module docstring plus migration `0024`.

Deliberately not fixed in code. A stub cannot satisfy a line-number citation, and
editing the three citations would diverge `roles.py` and — worse — migration `0024`'s
docstring from blackbird's copies. Migration files are what people diff across
deployments when debugging a schema mismatch, and a comment-only delta there is noise
that reads as signal. It would also guarantee a conflict on the next port in either
direction. Recording it here costs nothing and puts the answer where a reader chasing
the citation should end up.

The org1-specific role facts that would otherwise have lived in that document:

- The role mechanism ships, but **only `pi_lab` is ever assigned** on org1. No
  `AgentRegistry` row is set to `scout_hub`, and `prompts/roles/scout_hub/` is
  deliberately absent, not missing.
- `roles.DEFAULT_TOOLS` is exactly the four base tools
  (`retrieve_profile`, `retrieve_abstract`, `retrieve_full_text`, `retrieve_foa`), which
  is precisely cohort's `TOOL_DEFINITIONS`. The allow-list therefore grants and removes
  nothing. It is explicit rather than "every tool" so that a newly added tool stays
  opt-in.
- `prompts/identity.md` keeps "at Scripps Research". This is a deliberate divergence
  from blackbird and is guarded by `tests/unit/test_agent_prompts.py` (§6).

## 9. Open items

1. **How long the 0019 rollback door stays open** (§5). A judgement call about how long
   to hold the pre-window dump as the viable rollback before the first NULL
   `agent_id` row makes `alembic downgrade` unusable.
2. **Whether to enable post-type enforcement when cohorts flip on**, and whether that
   change authors an org1 prompt variant or keeps prompts frozen and accepts a menu
   that never renders (§7.1).
3. **`copi-prod`'s future.** After this branch merges to `main`, `copi-prod` should
   either be deleted or reduced to a deploy tag, so a fifth line does not re-accumulate.
