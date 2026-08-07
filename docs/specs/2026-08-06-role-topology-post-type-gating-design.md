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
   `src/agent/simulation.py:3663` is correct, but `start()` calls the builder at `:508` and
   `_recompute_allowed_sender_ids()` only at `:533`. Every gate is still `None`
   (`src/agent/agent.py:85`), so the filter no-ops. On a no-change roster tick the gate is
   recomputed (`:4551`) but the directory rebuilds only `if role_changed` (`:4549`) — so it is
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

3. **`tagged_agent` is never validated** — only logged (`simulation.py:2424`).

4. **The mention is stripped and the post ships anyway.** `_strip_disallowed_tags`
   (`:2539`, applied from `_post_message` at `:3359`) removes it, `_post_message` (`:3299`) posts the remainder. End-to-end from
   production: `{"post_type":"idea_crosslab","tagged_agent":"pearce"}` with body
   `:bulb: Idea — @PearceBot, your recent finding…` became `:bulb: Idea —, your recent finding…`.

5. **The strip regex requires the `Bot` suffix** (`:2596`), so `@pearce`-style tags bypass it
   entirely — the 12 leaked tags.

6. **Strips are logged at DEBUG** (`:2585`) under `level=INFO` (`src/agent/main.py:23`), so 200
   of them produced no operator-visible signal.

### Why prompt text cannot fix this

`post_type` is read once (`:2203`) and compared twice — `== "funding_collab"` (`:2230`),
`== "opportunity_assessment"` (`:2383`). No enum, no allow-list, no rejection, never persisted.
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
| `None` (mesh, or isolation off) | **No filtering by reachability.** A `targets` type is still dropped when no agent of a matching role exists on the roster — that is what removes `pitch` in a hubless mesh. Layer 3 is skipped outright. |
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

Three sub-rules, each chosen so the failure mode is proportionate:

- **A tag on a broadcast type is not an error if the tag is reachable.** `opportunity_assessment`
  addresses no one, but it is posted into the PI's own channel and the natural thing for the model
  to do is name that PI. Rejecting it would destroy the most valuable artifact in the system —
  and the whole interview behind it — over a field that is merely redundant. The tag is simply
  ignored for routing; the text mention survives because the PI is in the hub's gate. An
  *unreachable* tag on a broadcast type is still a rejection.
- **A `targets` type with `tagged_agent: null` is a rejection.** An addressed post that addresses
  no one is exactly the dangling ask this work exists to stop.
- **Layer 3 is fully inert when the gate is `None`.** Not "inert for reachable agents" — skipped
  entirely, including the null check. A mesh deployment's phase-5 behaviour must be
  byte-identical after this change, and today a hallucinated `tagged_agent` there is logged and
  the post ships. Tightening that is a separate decision, not a side effect of this one.

### Enabling fix — directory ordering

Make the directory a *derived product of the gate*: `_recompute_allowed_sender_ids` refreshes it
on every path it takes, including the two that disable gating. The three existing
`_build_lab_directories()` call sites (`:508`, `:4549`, `:4594`) then reduce to one — the
role-change branch, where the directory's contents move without the gate moving.

An earlier draft proposed keying the rebuild on the gate signature `_recompute_allowed_sender_ids`
computes at `:4690`. Rejected: that signature is `(cohort_count, len(rows), gated_count,
isolated)`, which is a *logging* fingerprint, not a per-agent one — two topologies with the same
counts but different memberships share it. Refreshing unconditionally is O(agents) over in-memory
profiles with no I/O, on a 30-second cadence.

Without this, layer 2 filters the menu correctly while the prompt still advertises 51
unreachable labs, and the model keeps working that roster as a backlog. Its own reasoning shows
this: *"I've already covered … Srinivasan/malaria, Weeraratna/melanoma … Let me look at labs I
haven't engaged with yet."*

### Enabling fix — the `funding_collab` bypass must not survive on the reply path

§1 records that a blocked agent can self-declare `funding_collab` to bypass the proposal block.
Layers 1–3 do not close it, because they govern `action: "new_post"` only and the bypass at
`simulation.py:2230` reads `post_type` regardless of action:

```python
is_funding_post = post_type == "funding_collab"
```

So `{"action": "reply", "target_post_id": <any non-funding thread>, "post_type":
"funding_collab"}` still walks past the block. The fix is one clause — `action == "new_post" and
post_type == "funding_collab"` — and it belongs in this change rather than a later one, because
this is the change that makes `post_type` a load-bearing, enforced field. A funding *reply* is
already covered by `is_funding_reply` on the line above, which checks the thread rather than the
model's self-declaration.

### The menu must never enumerate an empty list

`render_menu` describes each available type and, for a type with `targets`, names the agents it
may address. When the gate is `None` there is no enumeration to make — the mesh has 50 reachable
labs and listing them in every phase-5 prompt would recreate the 46 KB lab directory this design
is shrinking. So the addressed-type line has two forms:

| Gate | Rendering for a type with `targets` |
|---|---|
| Set | "Set `tagged_agent` to exactly one of: …" — the enumerated reachable agents |
| `None` | Guidance only: address one agent of the matching role, by its `agent_id` |

This is not only a mesh nicety. Without it, the "no topology supplied" default path renders
`Set tagged_agent to exactly one of: .` — an empty enumeration — and that string reaches a
committed characterization snapshot, because `test_phase5_prompt_gm` calls `build_phase5_prompt`
with no menu.

### Prompt changes

- A new `## Post types available to you this turn` section carries a `{post_type_menu}` token,
  placed immediately before `## Instructions`. Option C's body defers to it instead of hardcoding
  four types.

  **Single source of truth.** The menu is rendered from, and enforcement uses, one computed set:
  the role's declared `post_types`, filtered by layer 2, then further restricted when the agent is
  blocked for regular posts. Menu and enforcement cannot drift because they are one value —
  `post_types.available_for(...)`.

  **The restriction is keyed on `blocked_for_regular`, not on `funding_only`.** Those differ:
  `funding_only = blocked_for_regular and not has_available_non_funding` (`simulation.py:2107`),
  so a blocked agent that *does* have a non-funding post available gets `funding_only=False`.
  Keying the menu on `funding_only` would advertise `paper` and `pitch` to that agent and then
  have the block at `:2230` reject the post anyway — the exact prompt-versus-enforcement
  disagreement this design removes, reintroduced under a new name. `funding_only` continues to
  drive the template surgery; only the menu/enforcement set uses `blocked_for_regular`.

  The `### Option C: Make a new top-level post` heading and the intro paragraph stay
  byte-identical so `funding_only`'s existing regex surgery (`src/agent/agent.py:599-634`) still
  matches. Verified against the drafts: all four surgeries and the intro replacement match, and
  after the surgery the menu token survives while Option C is stripped and Option B kept. The
  new token is added to the raw-template pins at `tests/unit/test_roles.py:160-227`.
- Introduce the hub in the `pi_lab` prompt. Required by the `pitch` type: "Blackbird" currently
  appears nowhere in a spoke's prompt, profile, or directory.
- **Give `pitch` its own quality bar and its own worked example.** The existing bar asks for "a
  specific dataset, technique, or reagent each lab would contribute" and "a concrete first
  experiment" — correct for `idea_crosslab`, unfollowable for a pitch, whose counterparty has no
  bench and contributes nothing. One bar for two types is how the old prompt ended up asking for
  a tag the topology forbade.
- **Restore the `paper` preference explicitly.** The old Option C called `:newspaper:` "the
  PREFERRED post type — always consider sharing a paper first". Deferring Option C to the menu
  drops that clause, and `paper` is the one type with a measured 9.0% reply rate against 0.8%.
- **`pi_lab` phase-4 needs a hub-thread exception.** A `pitch` opens a thread between a PI bot
  and the hub, and the PI bot runs the `pi_lab` EXPLORE/DECIDE/CONCLUDE strings from
  `src/agent/thread_guidance.py`, which tell it to build toward a `:memo:` Summary naming "what
  each lab brings". Against a hub with no lab that is unfollowable. Those strings are pinned
  byte-for-byte by the snapshots and must not be reworded, so the exception goes in
  `prompts/phase4-thread-reply.md`.
- **Both roles' phase-2 scan filters must say "tags a specific agent *other than you*".** The
  current wording — "tags a specific other agent … that post is directed at them, not at you" —
  is the only thing between a `pitch` and being filtered out by its own recipient. Phase 3's
  tag auto-activation (`simulation.py:1114`) is the primary delivery path and does not depend on
  the scan, so this is belt-and-braces; it is one word, and the failure it prevents is the whole
  feature silently not working.
- **`scout_hub` has no `phase2-scan-filter.md` or `phase2-prune.md` override**, so it runs the
  `pi_lab` versions: "relevant to *your lab's* core expertise", "Papers *your own lab* authored",
  "labs whose capabilities complement yours". The hub has no lab. This predates the change and
  does not block it, but phase 2 is what decides which PIs get interviewed at all, so the drafts
  include overrides. Separable — recorded here so the reviewer can drop them without unpicking
  anything else.
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

Draft prompt files for review: `docs/specs/2026-08-06-post-type-gating-prompts-draft/`. That
directory mirrors `prompts/` and holds the **complete** set each role resolves — 7 changed `.md`
files plus `role.toml`, the 2 new `scout_hub` overrides, and verbatim copies of the 3 that do not
change — so
a reviewer reads a bot's whole instruction set rather than reconstructing it from diffs. Its
`README.md` is the change-by-change rationale.

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

Placement: inside the existing `else:` (new top-level post) branch, before `_post_message`. Note
`consecutive_phase5_skips` is zeroed earlier in the handler, before the branch, so a rejection
must re-increment it.

### ⚠️ Do not trust a line number in this document

`blackbird` takes concurrent commits from other work, and `simulation.py` has moved twice during
this document's life alone: the original references were written before `f7a9f68`/`a247ed8` (2-3
lines low), and then `e116feb`, `44f09be`, `c6943d4`, `f32a83e` and `517a564` moved everything after `:1366` again — by up to 46 lines. Re-verified at `517a564`: `agent.py`, `roles.py` and `prompts/` were untouched by all five, so only `simulation.py` numbers moved.
Every code reference here is also quoted verbatim; **the quote is the anchor, the number is
decoration.** If they disagree, the quote wins.

Values re-verified at `517a564` (HEAD), and the command that re-derives them:

| Symbol | Line |
|---|---|
| `_phase5_new_post` | 1956 |
| `blocked_for_regular = …` | 1983 |
| `funding_only = …` | 2126 |
| `build_phase5_prompt` call | 2128 |
| `post_type = action_data.get(…)` | 2203 |
| `is_funding_post = …` (the bypass) | 2230 |
| `_strip_disallowed_tags` call | 2254 |
| new-post `else:` branch | 2360 |
| `_strip_disallowed_tags` def / DEBUG / regex | 2539 / 2585 / 2596 |
| `_post_message` def | 3299 |
| `_build_lab_directories` def | 3638 |
| `_build_lab_directories()` call sites | 508, 4549, 4594 |
| `_recompute_allowed_sender_ids()` call sites | 533, 4551, 4604 |
| `_disable_all_gates()` call sites | 4633, 4675 |
| `_apply_cohort_gate_to_state()` call sites | 4639, 4677, 4712 |

```bash
grep -n "def _phase5_new_post\|blocked_for_regular = \|funding_only = \|\
build_phase5_prompt(\|is_funding_post = \|def _build_lab_directories\|\
_build_lab_directories()\|_recompute_allowed_sender_ids()\|_disable_all_gates()" \
  src/agent/simulation.py
```

## 5. Error handling

Every failure mode fails toward "post nothing" rather than "post something wrong", except
where that would silence a deployment.

| Condition | Behaviour |
|---|---|
| No `post_types` in `role.toml` | `DEFAULT_POST_TYPES`, WARNING once |
| Malformed entry (not a table, or no `name`) | Drop that entry, WARNING, keep the rest |
| Unknown `post_type` name | Drop it, WARNING — mirrors `roles.py:99-103` for tools |
| `targets` names a nonexistent role | Type never offered, WARNING at load (catches typos) |
| Gate is `None` | Layer 2 does not filter; layer 3 is skipped entirely, including its null check |
| Layer 2 filters the menu to empty | Render an explicit "none available" menu and reject any `new_post`; do **not** skip the turn |
| Menu has a `targets` type but no gate to enumerate from | Render guidance, never `one of: .` — see §3 |
| Counterparty role unknown | Matches no `targets` |
| Broadcast type carrying a reachable `tagged_agent` | Ignore the tag, publish — see §3 layer 3 |
| Broadcast type carrying an unreachable `tagged_agent` | Reject |
| Malformed `role.toml` overall | Existing behaviour: log ERROR, use defaults, never raise |

The empty-menu row deserves care. An earlier draft of this design said "skip the turn", which is
wrong: the menu governs `action: "new_post"` only, so skipping would also suppress a legitimate
`action: "reply"` — exactly the funding replies that are the only thing a blocked spoke can still
do. Instead the menu renders an explicit "no new top-level post type is available to you this
turn — reply or skip", and enforcement rejects only an actual `new_post`.

In normal mode the menu cannot empty out anyway, because the broadcast types carry no `targets`.
It can and does empty for a blocked spoke in the star, where `funding_collab` is the only
new-post candidate and has no reachable `pi_lab` — which is precisely the case that must still
leave Option A open.

## 6. Testing

The bug survived because `tests/unit/test_simulation_logic.py:1104` pre-seeds
`allowed_sender_ids` by hand at `:1110-1115` before calling the builder, and
`tests/unit/test_roster_sync.py:108` stubs the builder out entirely. Priority is therefore
tests that exercise **production order**, not just the predicate.

1. **Ordering regression** — the directory is gate-scoped after `start()`'s real sequence.
   `start()` does too much I/O to drive in a unit test, so this is two tests, not one: a source
   assertion that `_recompute_allowed_sender_ids` precedes the directory rebuild in `start()`,
   and a behavioural one on the durable half of the fix (below). A one-shot shell check run
   during implementation is not a regression test — the bug it guards is a *reordering*, which
   is exactly the kind of edit a future refactor makes silently.
2. **Gate-change rebuild** — the directory refreshes when the gate signature changes, not only
   on roster churn. Must be driven **through `_recompute_allowed_sender_ids` itself**, not by
   calling the rebuild by hand: calling it by hand tests the predicate, which was never broken.
   The cheap version is the isolation-disabled path, which needs no DB.
3. **Layer 2 truth table** — parametrised over star / mesh / gate-off: `idea_crosslab` drops in
   star and stays in mesh; `pitch` the inverse.
4. **Layer 3** — `tagged_agent="pearce"` from `markham` is rejected, using the real production
   JSON as the fixture.
5. **No-Slack-call assertion** — a rejected post must not call `post_message` and must not
   increment `message_count`. This is what makes "reject" honest rather than cosmetic, and it
   only earns that description if it drives **`_phase5_new_post` end to end** against a canned
   LLM response. A test that calls the rejection helper directly and then asserts a stubbed
   `_post_message` was not called proves nothing — the helper never calls it either way, so the
   test passes just as happily when the call site was never wired up.
   `tests/unit/test_simulation_logic.py:1166` (`TestPhase5ReplyActionSuppression`) is the working
   pattern: stub `build_phase5_prompt`, monkeypatch `generate_agent_response`, `await
   engine._phase5_new_post(agent)`, assert on `FakeSlackClient.posted`.
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
11. **The default menu is role-aware.** `build_phase5_prompt(post_type_menu=None)` must render
    the *calling agent's* role set, not `DEFAULT_POST_TYPES` unconditionally — otherwise a
    `scout_hub` agent built by any direct caller is handed a menu offering `paper`,
    `idea_crosslab` and `pitch`, none of which its `role.toml` allows.
12. **No enumeration is ever empty.** `render_menu` on a `targets` type with nothing to enumerate
    must not emit `one of: .` — the case that otherwise reaches a committed snapshot.
13. **The reply-path funding bypass** — a blocked agent's `{"action": "reply", "post_type":
    "funding_collab"}` to a non-funding thread is blocked.

### Characterization snapshots will legitimately change — 8 of 9

Measured, not assumed. `agent-system.md` and `identity.md` are injected into **every** phase's
system prompt, so editing them moves almost every snapshot in
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr`. Two more move because the drafts
now also edit `phase2-scan-filter.md` and `phase4-thread-reply.md`, whose bodies these tests
capture in `messages`, not just in `system`:

| Snapshot | Why it changes |
|---|---|
| `test_scan_system_prompt_gm` | `Scripps Research`, `:test_tube:`, `:package:` (system only) |
| `test_system_prompt_public_vs_private_gm` | same |
| `test_thread_reply_system_prompt_gm` | same |
| `test_phase2_scan_prompt_flags_self_authored_gm` | the above **plus** the two new `phase2-scan-filter.md` exclusion rules |
| `test_phase4_prompt_phase_progression_gm` | the above **plus** the new `### If the other party is a scouting hub` section |
| `test_phase4_prompt_pi_context_and_funding_gm` | same as the row above |
| `test_reply_turn_composes_prompt_and_posts_gm` | same as the row above |
| `test_phase5_prompt_gm` | the system-prompt changes **plus** the whole Option C rewrite, the menu section, and the rendered default menu |

Only `test_decide_phase_parses_scripted_json_gm` is unaffected.

This does **not** license a blanket `pytest --snapshot-update`. CLAUDE.md's prohibition exists to
stop unintended drift being papered over, and it still binds. The rule for this change:

- Regenerate those eight snapshots deliberately, then **read the diff line by line**.
- The diff must contain *only* text originating in the edited prompt files.
- The EXPLORE / DECIDE / CONCLUDE guidance strings from `src/agent/thread_guidance.py` must
  appear **unchanged** in the diff. They are not touched by this work, and any movement in them
  means something else broke.
- Baseline before starting: re-verified at `517a564`, after all five concurrent commits — `pytest
  tests/characterization/test_agent_turn_gm.py tests/unit/test_roles.py
  tests/unit/test_agent_prompts.py` gives **38 passed, 9 snapshots passed** (251s; the time is
  testcontainers bringing up Postgres). Re-run it before touching a snapshot: a snapshot moving
  for a reason that predates your change is the failure mode this baseline exists to rule out.

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
- **Cohort strips are invisible at INFO.** Raising `simulation.py:2585` to INFO, or surfacing
  the counter, is what makes the next occurrence of this class of bug visible in hours instead
  of never.
- **Stale docstring path:** 21 citations across 9 files reference `.notes/cohort-system-v2.md`;
  the file is at `specs/cohort-system-v2.md`.
- **`SLACK_ENABLED=true`** while the runbook's Phase 4 specifies DB-only for confidentiality,
  which reopens runbook risk **A4** (the gate is behaviour, not access control).
- **Two Pearce labs:** `pearce` is Erika Pearce, `epearce` is Edward Pearce.
