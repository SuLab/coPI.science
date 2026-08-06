# Role- and topology-aware post-type gating

**Date:** 2026-08-06
**Status:** Design, approved. Not implemented.
**Branch:** `blackbird`

## 1. The problem, measured

In the live star topology, 56 cohorts each hold `{<pi>, blackbird, grantbot}`, so no `pi_lab`
agent may interact with any other `pi_lab` agent. Yet in the latest simulation run:

| Metric | Value |
|---|---|
| `:bulb:` Idea top-level posts | 259 |
| …carrying the tag-strip artifact (`Idea —,`) | 200 (77%) |
| …leaking an `@agent_id`-style cross-cohort tag to Slack | 12 |
| …naming the lab in prose only, no tag | 42 |
| …mentioning `blackbird`, the one reachable partner | **0** |
| …that received any reply | **2 (0.8%)** |
| `:newspaper:` Paper posts (control) | 234, **0** artifacts, 21 replies (9.0%) |
| Phase-5 posts declaring `tagged_agent` (13h container run) | 146 |
| …targeting `blackbird` or `grantbot` | **0** |

The hub's interview pipeline is seeded 20 threads from `:newspaper:` Paper versus 2 from
`:bulb:` Idea. Ideas are 53% of top-level posts and 9% of the pipeline.

### Root cause chain

1. **`_build_lab_directories` runs before the cohort gate exists.** The filter at
   `src/agent/simulation.py:3615` is correct, but `start()` calls the builder at `:508` and
   `_recompute_allowed_sender_ids()` only at `:533`. Every gate is still `None`
   (`src/agent/agent.py:85`), so the filter no-ops. On a no-change roster tick the gate is
   recomputed (`:4502`) but the directory rebuilds only `if role_changed` (`:4500`) — so it is
   never rebuilt against a live gate. Verified: zero roster add/remove and zero role changes in
   the 13-hour run.

   Production confirmation from a stored `llm_call_logs.system_prompt`: `gill`'s phase-5 prompt
   names **51 labs**, all unreachable, and the string "Blackbird" appears **nowhere**. The
   directory is **69%** of the 67 KB system prompt.

   This is runbook gap **A3**, which
   `docs/specs/2026-08-05-hub-bot-customization-design.md:261` records as closed. The guard was
   written; the call ordering makes it dead code.

2. **The prompt then demands a tag.** `prompts/phase5-new-post.md:129-131` — "TAG the other
   lab's agent (e.g., @WisemanBot)".

3. **`tagged_agent` is never validated** — only logged (`simulation.py:2375`).

4. **The mention is stripped and the post ships anyway.** `_strip_disallowed_tags`
   (`:2490`) removes it, `_post_message` (`:3310`) posts the remainder. End-to-end from
   production: `{"post_type":"idea_crosslab","tagged_agent":"pearce"}` with body
   `:bulb: Idea — @PearceBot, your recent finding…` became `:bulb: Idea —, your recent finding…`.

5. **The strip regex requires the `Bot` suffix** (`:2547`), so `@pearce`-style tags bypass it
   entirely — the 12 leaked tags.

6. **Strips are logged at DEBUG** (`:2536`) under `level=INFO` (`src/agent/main.py:23`), so 200
   of them produced no operator-visible signal.

### Why prompt text cannot fix this

`post_type` is read once (`:2185`) and compared twice — `== "funding_collab"` (`:2212`),
`== "opportunity_assessment"` (`:2346`). No enum, no allow-list, no rejection, never persisted.
Two consequences today: a blocked agent can self-declare `funding_collab` to bypass the
proposal block, and any `pi_lab` agent declaring `opportunity_assessment` writes an
`OpportunityAssessment` row with no role check.

More fundamentally, the constraint is **topological, not role-intrinsic**: `pi_lab` in org1's
mesh *should* make cross-lab idea posts — that is the product. The same role in this star must
not. A prompt cannot know which deployment it is in.

## 2. Canonical vocabulary

Today's vocabulary cannot support an allow-list. `idea` and `idea_crosslab` are both in the
enum (`prompts/phase5-new-post.md:169`) with no documented difference and no code
distinguishing them; `:question:`, `:test_tube:` and `:package:` are offered as labels
(`prompts/agent-system.md:219-221`) with no `post_type` at all; `:memo:` and `:mag:` are the
most consequential markers and are absent from the enum.

| `post_type` | Emoji | `targets` | pi_lab | scout_hub |
|---|---|---|---|---|
| `paper` | `:newspaper:` | — | ✓ | |
| `help_wanted` | `:sos:` | — | ✓ | |
| `introduction` | `:wave:` | — | ✓ | |
| `idea_crosslab` | `:bulb:` | `["pi_lab"]` | ✓ | |
| `pitch` | `:bulb:` | `["scout_hub"]` | ✓ | |
| `funding_collab` | `:moneybag:` | `["pi_lab"]` | ✓ | ✓ |
| `opportunity_assessment` | `:mag:` | — | | ✓ |

Resolutions: collapse `idea` into `idea_crosslab`; add `pitch` (a spoke pitching its own
commercializable idea to the hub); drop `:test_tube:` and `:package:` from the `pi_lab` label
table (unused, undefined); mark `:question:` reply-only, which both roles' prompts already
imply. `:memo:` stays outside this vocabulary — it is a thread-reply marker, not a top-level
type.

**Scope: the allow-list governs `action: "new_post"` only.** `action: "reply"` is untouched.

## 3. Architecture — three layers

New module `src/agent/post_types.py`, dependency-free like `roles.py` and `thread_guidance.py`,
so the filter is unit-testable without a DB, an engine, or a running loop.

### Layer 1 — declarative allow-list in `role.toml`

Mirrors the existing `tools` key and is enforced the two ways `tools_for_role` is
(`src/agent/tools.py:128` filters what the model sees; `:148` refuses at dispatch).

Shape of the key (illustrative — `targets` names the `AgentRegistry` roles a type may
address; empty or absent means the type addresses no one):

```toml
[[post_types]]
name = "paper"

[[post_types]]
name = "idea_crosslab"
targets = ["pi_lab"]

[[post_types]]
name = "pitch"
targets = ["scout_hub"]
```

An absent `post_types` key yields `DEFAULT_POST_TYPES`, explicit for the same reason
`DEFAULT_TOOLS` is (`roles.py:22-27`): a newly added type must stay opt-in rather than being
handed to every role silently.

`pi_lab` has no `role.toml` today — "pi_lab is the absence of overrides" (`roles.py:58-60`).
`DEFAULT_POST_TYPES` therefore *is* pi_lab's list, and `prompts/roles/pi_lab/` still need not
exist.

### Layer 2 — topology filter

Keep a post type iff its `targets` is empty, **or** there exists an agent in the acting
agent's `allowed_sender_ids`, excluding itself, whose `AgentRegistry.role` is in `targets`.

| Gate state | Behaviour |
|---|---|
| `None` (mesh, or isolation off) | **No filtering.** Layers 2 and 3 are no-ops |
| Set | Filter as above |

The `None` case is what preserves org1's mesh byte-for-byte, and it is the same way every
other cohort feature degrades.

Worked results:

- **Star.** `gill`'s gate is `{gill, blackbird, grantbot}`. Excluding self, roles are
  `blackbird=scout_hub` and `grantbot=`(no `AgentRegistry` row → unknown). No `pi_lab` peer, so
  `idea_crosslab` and `funding_collab` drop; `blackbird` is `scout_hub`, so `pitch` appears.
- **Mesh.** Lab peers exist, so `idea_crosslab` stays; no `scout_hub` exists, so `pitch` drops
  automatically with no per-deployment configuration.

An unknown counterparty role matches no `targets`. That is correct for `grantbot`, which is a
funding announcer, not a pitch recipient.

### Layer 3 — `tagged_agent` validation

Reject the post when `tagged_agent` is set and either is not in `allowed_sender_ids`, or its
role is not in the chosen type's `targets`. This is the layer that would have stopped all 146
posts, and it closes the `@pearce` bypass because it validates the JSON field rather than
scanning prose.

### Enabling fix — directory ordering

Move `_build_lab_directories()` after `_recompute_allowed_sender_ids()` at all three call sites
(`:508`/`:533`, `:4500`/`:4502`, `:4545`/`:4555`), and rebuild whenever the gate signature
changes — `_recompute_allowed_sender_ids` already computes that signature at `:4641`, so the
hook exists.

Without this, layer 2 filters the menu correctly while the prompt still advertises 51
unreachable labs, and the model keeps working that roster as a backlog. Its own reasoning shows
this: *"I've already covered … Srinivasan/malaria, Weeraratna/melanoma … Let me look at labs I
haven't engaged with yet."*

### Prompt changes

- A new `## Post types available to you this turn` section carries a `{post_type_menu}` token,
  placed immediately before `## Instructions`. Option C's body defers to it instead of hardcoding
  four types.

  **Single source of truth.** The menu is rendered from, and enforcement uses, one computed set:
  the role's declared `post_types`, filtered by layer 2, then further restricted by
  `funding_only` when that mode applies. Menu and enforcement cannot drift because they are one
  value — `post_types.available_for(agent, funding_only=…)`.

  The `### Option C: Make a new top-level post` heading and the intro paragraph stay
  byte-identical so `funding_only`'s existing regex surgery (`src/agent/agent.py:599-633`) still
  matches. Verified against the drafts: all four surgeries and the intro replacement match, and
  after the surgery the menu token survives while Option C is stripped and Option B kept. The
  new token is added to the raw-template pins at `tests/unit/test_roles.py:160-227`.
- Introduce the hub in the `pi_lab` prompt. Required by the `pitch` type: "Blackbird" currently
  appears nowhere in a spoke's prompt, profile, or directory.
- **Institution:** `prompts/agent-system.md:3` and `prompts/identity.md:2` say "Scripps
  Research". Measured: 57 of 60 public profiles say "Johns Hopkins University"; the only
  "Scripps Research Institute" is `alanjary.md`, the test bot being disabled;
  `mukherjeeclavin.md` and `pearce.md` name no institution. So the identity line becomes
  **institution-neutral** and lets the public profile — injected directly below it — carry that
  fact. Strictly more correct than either literal and needs no new per-agent field.

  **Coupled code change:** `_DEFAULT_IDENTITY` (`src/agent/agent.py:753`) is a fallback that must
  match `prompts/identity.md` verbatim, *including the absence of a trailing newline* — see the
  comment at `:750` and `_compose_system_prompt`, which depends on exactly one blank line between
  blocks. Edit both or neither. The draft preserves the missing trailing newline (verified with
  `xxd`).

Draft prompt files for review: `docs/superpowers/specs/prompts-draft/`.

## 4. Data flow

```
LLM returns {action, post_type, tagged_agent}
   │
   ├─ action == "reply"  ─────────────────────────►  unchanged path
   │
   └─ action == "new_post"
        ├─ L1: post_type in role's declared set?        ─ no ─► reject
        ├─ L2: targets satisfiable from gate?           ─ no ─► reject
        ├─ L3: tagged_agent in gate & role in targets?  ─ no ─► reject
        └─ yes → _post_message  (existing path, unchanged)

reject = no Slack call, no message_count++, WARNING log,
         consecutive_phase5_skips++
```

Placement: inside the existing `else:` (new top-level post) branch at `simulation.py:2324`,
before `_post_message`. Note `consecutive_phase5_skips` is zeroed at `:2180`, before the
branch, so a rejection must re-increment it.

## 5. Error handling

Every failure mode fails toward "post nothing" rather than "post something wrong", except
where that would silence a deployment.

| Condition | Behaviour |
|---|---|
| No `post_types` in `role.toml` | `DEFAULT_POST_TYPES`, WARNING once |
| Malformed entry (not a table, or no `name`) | Drop that entry, WARNING, keep the rest |
| Unknown `post_type` name | Drop it, WARNING — mirrors `roles.py:99-103` for tools |
| `targets` names a nonexistent role | Type never offered, WARNING at load (catches typos) |
| Gate is `None` | Layers 2 and 3 are no-ops |
| Layer 2 filters the menu to empty | Skip the turn, WARNING — never send an empty menu |
| Counterparty role unknown | Matches no `targets` |
| Malformed `role.toml` overall | Existing behaviour: log ERROR, use defaults, never raise |

The empty-menu row is the one real hazard in the `pitch` design: were the hub to go inactive,
every spoke's directed types would vanish. Broadcast types carry no `targets`, so the menu
cannot actually empty out; the guard remains as belt-and-braces.

## 6. Testing

The bug survived because `tests/unit/test_simulation_logic.py:1104` pre-seeds
`allowed_sender_ids` by hand at `:1110-1115` before calling the builder, and
`tests/unit/test_roster_sync.py:108` stubs the builder out entirely. Priority is therefore
tests that exercise **production order**, not just the predicate.

1. **Ordering regression** — the directory is gate-scoped after `start()`'s real sequence.
   Currently fails; that is the point.
2. **Gate-change rebuild** — the directory refreshes when the gate signature changes, not only
   on roster churn.
3. **Layer 2 truth table** — parametrised over star / mesh / gate-off: `idea_crosslab` drops in
   star and stays in mesh; `pitch` the inverse.
4. **Layer 3** — `tagged_agent="pearce"` from `markham` is rejected, using the real production
   JSON as the fixture.
5. **No-Slack-call assertion** — a rejected post must not call `post_message` and must not
   increment `message_count`. This is what makes "reject" honest rather than cosmetic.
6. **`load_role` degradation** — one test per row in §5.
7. **Menu/enforcement agreement** — the rendered `{post_type_menu}` names exactly the
   post-layer-2 set.
8. **Mesh behaviour** — `pi_lab` with gate `None` still offers every declared type and rejects
   nothing. Layers 2 and 3 must be provably inert.
9. **Token pins** — `{post_type_menu}` must be added to the `leftover_tokens` list at
   `tests/unit/test_roles.py:178` and the renderer-anchor list at `:370`. Without that, a
   template carrying the token while the renderer does not substitute it would pass CI and leak
   the raw `{post_type_menu}` into a live prompt.
10. **A new pi_lab phase-5 token/surgery pin.** Measured: `test_roles.py:160-227` pins the
    scout_hub override only — the *global* `pi_lab` template's tokens and `funding_only`
    surgeries are pinned nowhere. Since this change rewrites that template, add the equivalent
    test for `pi_lab`.

### Characterization snapshots will legitimately change — 8 of 9

Measured, not assumed. `agent-system.md` and `identity.md` are injected into **every** phase's
system prompt, so editing them moves almost every snapshot in
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr`:

| Snapshot | Why it changes |
|---|---|
| `test_scan_system_prompt_gm` | `Scripps Research`, `:test_tube:`, `:package:` |
| `test_system_prompt_public_vs_private_gm` | same |
| `test_thread_reply_system_prompt_gm` | same |
| `test_phase2_scan_prompt_flags_self_authored_gm` | same |
| `test_phase4_prompt_phase_progression_gm` | same |
| `test_phase4_prompt_pi_context_and_funding_gm` | same |
| `test_reply_turn_composes_prompt_and_posts_gm` | same |
| `test_phase5_prompt_gm` | the above **plus** `Quality bar for :bulb:`, `TAG the other lab`, `@WisemanBot` |

Only `test_decide_phase_parses_scripted_json_gm` is unaffected.

This does **not** license a blanket `pytest --snapshot-update`. CLAUDE.md's prohibition exists to
stop unintended drift being papered over, and it still binds. The rule for this change:

- Regenerate those eight snapshots deliberately, then **read the diff line by line**.
- The diff must contain *only* text originating in the three edited prompt files.
- The EXPLORE / DECIDE / CONCLUDE guidance strings from `src/agent/thread_guidance.py` must
  appear **unchanged** in the diff. They are not touched by this work, and any movement in them
  means something else broke.
- Baseline before starting: these nine snapshots pass today (verified — 130 tests, 9 snapshots
  green across `test_agent_turn_gm`, `test_roles`, `test_agent_prompts`, `test_simulation_logic`,
  `test_roster_sync`, `test_tool_gating`).

### Prompt and code must land in the same change

`prompts/` is bind-mounted into the agent container and re-read per call
(`docker inspect` confirms the mount; `agent.py:744` reads from disk with no cache), while
`src/` is **baked into the image**. So installing the prompt drafts without rebuilding the agent
image would put `{post_type_menu}` in front of a live renderer that cannot substitute it — the
raw token would reach real prompts. Ship the template edit and the renderer together, and
rebuild the agent image (`$DC --profile agent build agent`) before the next run.

Gate: `./scripts/ci.sh`. No migration — `post_types` is config and `AgentRegistry.role` already
exists.

## 7. Operational items

These are one-off operations, not part of the code change. They are recorded here because the
cutover depends on their ordering.

| Item | Method | Reversible |
|---|---|---|
| Disable `alanjary` (test-only bot) | `status='inactive'` in `AgentRegistry`; picked up live by `_sync_roster_from_db`, no restart | Yes |
| Delete its orphan cohort | `hub-alanjary` holds only `blackbird`+`grantbot`; the PI was never a member, so the agent is currently isolated to nobody | Yes |
| Clean working memory | Move `profiles/memory/*` to a timestamped backup — never `rm` | Yes |
| Delete mutilated Slack posts | **113 messages** (see below), via each authoring bot's own token | **No** |

`--fresh` already wipes `agent_messages` (`src/agent/main.py:168`), so deleting the Slack copies
and then restarting `--fresh` leaves both sides consistent, with no orphaned DB rows breaking
the row-count-matches-Slack-message-count invariant documented at `_post_message`. `--fresh`
does **not** touch `profiles/memory/`, which is why the memory move is a separate step.

**The deletion set, measured.** 200 top-level `:bulb:` posts carry the strip artifact; **113 of
them reached Slack** (the other 87 are DB-only, written while Slack was off or before tokens were
provisioned). They span 43 authoring agents and 6 channels. Only the 113 can be deleted — and
only the 113 need to be, since the rest never became visible.

All measurements in this document come from a single simulation run,
`4f1e8395-8329-438d-99e8-d3bfeaa5ffb5` (started 2026-08-05 18:25 UTC, 671 messages). The agent
container was restarted at 21:50 UTC on 2026-08-06 and **resumed** that run rather than starting
a new one, so the counts remain current.

The deletion set requires explicit sign-off before it runs.

## 8. Out of scope, recorded

Found during investigation, not addressed here:

- **Working memory is not cohort-filtered** and names out-of-cohort labs on disk now (e.g.
  `profiles/memory/epearce/public.md` lists six unreachable partners). Survives `--fresh`.
  Mitigated for the next run by the memory move in §7, not fixed structurally.
- **`_prior_threads` is not cohort-filtered** (visibility only) and loads all `ThreadDecision`
  rows across runs (`simulation.py:4060`).
- **`retrieve_profile` is not cohort-checked** and its path is unsanitised
  (`src/agent/tools.py:233`). Verified reachable: `../private/blackbird` returns the hub's
  private screening rubric; `../../CLAUDE` returns `CLAUDE.md`. Latent — 0 of 8 calls in the run
  contained `..`. Wants a `resolve()`-and-check-parent guard.
- **`summarize_funding_thread(viewer_agent_id=…)` never reads that parameter** although both
  callers pass it (`src/agent/funding_rules.py:172`, `simulation.py:1320`, `:2062`); its
  `spinoffs` block scans the whole log (`funding_rules.py:211`).
- **Cohort strips are invisible at INFO.** Raising `simulation.py:2536` to INFO, or surfacing
  the counter, is what makes the next occurrence of this class of bug visible in hours instead
  of never.
- **Stale docstring path:** 21 citations across 9 files reference `.notes/cohort-system-v2.md`;
  the file is at `specs/cohort-system-v2.md`.
- **`SLACK_ENABLED=true`** while the runbook's Phase 4 specifies DB-only for confidentiality,
  which reopens runbook risk **A4** (the gate is behaviour, not access control).
- **Two Pearce labs:** `pearce` is Erika Pearce, `epearce` is Edward Pearce.
