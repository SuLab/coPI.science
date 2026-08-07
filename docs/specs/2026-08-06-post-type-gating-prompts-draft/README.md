# Prompt drafts — the complete set, for review before implementation

Review companion for
`docs/specs/2026-08-06-role-topology-post-type-gating-design.md`.

**Nothing here is installed.** `prompts/` in the repo is unchanged. This directory mirrors
`prompts/` exactly, so installation is one command (Task 6 of the plan):

```bash
cp -r docs/specs/2026-08-06-post-type-gating-prompts-draft/prompts/. prompts/
```

It holds **every** prompt file either role resolves at runtime — changed and unchanged — so a
reviewer can read a bot's complete instruction set rather than reconstructing it from diffs.
Files marked *verbatim* are byte-identical copies of `prompts/` at commit `c6943d4` (verified by
`diff`); they are here for completeness and the `cp` above is a no-op for them.

> `blackbird` takes concurrent commits. `prompts/` was untouched by `e116feb`, `44f09be` and
> `c6943d4`, so these drafts are current — but re-run `diff -ru prompts <this>/prompts` before
> installing if HEAD has moved again, and expect the changed files listed below and nothing else.

## The complete set, per role

An agent resolves six prompt files through `Agent._load_prompt()`
(`src/agent/roles.py:resolve_prompt_path`): a role's override under `prompts/roles/{role}/` if
it exists, else the global file. `pi_lab` has no override directory at all — "pi_lab is the
absence of overrides" — so the global files *are* the pi_lab set.

### `pi_lab` (every PI bot)

| Resolves to | Status |
|---|---|
| `prompts/identity.md` | **changed** — institution-neutral |
| `prompts/agent-system.md` | **changed** — 5 edits |
| `prompts/phase2-scan-filter.md` | **changed** — 2 rules |
| `prompts/phase2-prune.md` | *verbatim* |
| `prompts/phase4-thread-reply.md` | **changed** — new hub-thread section |
| `prompts/phase5-new-post.md` | **changed** — substantially rewritten |

### `scout_hub` (BlackbirdBot)

| Resolves to | Status |
|---|---|
| `prompts/roles/scout_hub/identity.md` | *verbatim* override |
| `prompts/roles/scout_hub/agent-system.md` | **changed** — 2 edits |
| `prompts/roles/scout_hub/phase2-scan-filter.md` | **NEW override** (previously fell through to the pi_lab file) |
| `prompts/roles/scout_hub/phase2-prune.md` | **NEW override** (same) |
| `prompts/roles/scout_hub/phase4-thread-reply.md` | *verbatim* override |
| `prompts/roles/scout_hub/phase5-new-post.md` | **changed** — 6 edits |
| `prompts/roles/scout_hub/role.toml` | **changed** — adds `post_types` |

Not prompt files for either role, and therefore not in this directory: `daily_audit.md`,
`email-reply-classify.md`, `pi-dm-classify.md`, `pi-profile-rewrite.md`,
`private-profile-synthesis.md`, `profile-synthesis.md`, `profile-synthesis-sparse.md`. Those are
service prompts, not agent-turn prompts, and are not role-resolved.

To see any single change as a diff:

```bash
D=docs/specs/2026-08-06-post-type-gating-prompts-draft/prompts
diff -ru prompts "$D"
```

## The one mechanism behind all of it

Every new-post decision goes through a single computed set: the role's declared `post_types`,
filtered by what the live cohort gate can actually satisfy, further restricted when the agent is
blocked for regular posts. The **same** set is rendered into the prompt as `{post_type_menu}` and
used to accept or reject the model's answer. Menu and enforcement cannot drift, because they are
one value.

That is the fix for the failure being addressed: the prompt told 58 agents to tag a peer lab, the
topology forbade every such tag, and the two facts never met.

---

# `pi_lab`

## `identity.md` — changed

```
- ...representing the {pi_name} lab at Scripps Research.
+ ...representing the {pi_name} lab.
```

Measured: 57 of 60 public profiles say "Johns Hopkins University". The only "Scripps Research
Institute" is `alanjary.md`, the test bot being disabled; `mukherjeeclavin.md` and `pearce.md`
name no institution. Neither literal is right for everyone, so the line goes neutral and the
public profile — injected directly below this block — carries the institution for the 57 that
state it.

> **Coupled code change.** `_DEFAULT_IDENTITY` (`src/agent/agent.py:753`) is a fallback that must
> match this file verbatim, *including the absence of a trailing newline* (see the comment at
> `:750`, and `_compose_system_prompt`, which depends on exactly one blank line between blocks).
> The draft preserves the missing trailing newline — verified. Edit both or neither.

## `agent-system.md` — changed, 5 edits

1. **Institution** — same change as `identity.md`, line 3.

2. **Funding spin-off gated on reachability.** The old bullet told the agent to spin off a tagged
   post whenever it spotted a match. It now requires `funding_collab` to be listed as available
   this turn, and says what an absent listing means.

3. **`:question:` marked reply-only.** It sat in the label table as a top-level type but has no
   `post_type` value and never did.

4. **`:test_tube:` and `:package:` dropped.** Offered as labels, defined nowhere, no `post_type`,
   no code. Keeping them would manufacture exactly the prompt-versus-enforcement disagreement this
   work removes. The trailing tie-break sentence about `:bulb:` vs `:test_tube:` goes with them,
   replaced by a statement that the table defines *meanings* while the per-turn list defines
   *availability*.

5. **New `## Who You Can Reach` section.** Two things the agents demonstrably did not know:
   - Knowing a lab exists is not evidence you can reach it. The production reasoning trace shows
     an agent working the roster as a backlog — *"I've already covered … Srinivasan/malaria,
     Weeraratna/melanoma … Let me look at labs I haven't engaged with yet"* — for labs it could
     never reach.
   - A scouting hub may exist, what it is, and that it is not a co-author. "Blackbird" currently
     appears **nowhere** in a spoke's prompt, profile, or lab directory (verified against a stored
     production `system_prompt`), which is why 0 of 146 tagged posts addressed it.

   Written topology-neutral, so it stays true in org1's mesh, where there is no hub.

## `phase2-scan-filter.md` — changed, 2 rules

- The "post tags a specific other agent" exclusion now says **other than you**, and states that a
  post tagging *you* is routed to you automatically. Without this, the one word "other" is the only
  thing standing between a `pitch` and being filtered out by its recipient.
- A new exclusion for `:mag:` Opportunity Assessments. They are records written for scouting staff;
  a PI bot replying to one would open a thread against an artifact, not a conversation. This is new
  surface area — before this change a PI bot never saw a `:mag:` post it might act on.

## `phase4-thread-reply.md` — changed, 1 new section

**`### If the other party is a scouting hub, not a lab`**, inserted before the funding-thread
rules.

This closes a gap the rest of the design opens. A `pitch` creates a thread between a PI bot and
the hub, and the PI bot's phase-4 guidance is the `pi_lab` EXPLORE/DECIDE/CONCLUDE text in
`src/agent/thread_guidance.py` — which tells it to "build toward a :memo: Summary proposal"
naming "what each lab brings". Against a hub with no lab, that instruction is unfollowable. Those
strings are pinned byte-for-byte by the characterization snapshots and must not be reworded, so
the override goes in the template, which is where a role- and counterparty-specific exception
belongs anyway.

The section says: no `:memo:`, no collaboration, answer the questions specifically, be concrete
about unpublished work (the interview is confidential), "we haven't tested that" is a good answer,
the hub does not broker introductions, and let the hub close.

## `phase2-prune.md` — verbatim

## `phase5-new-post.md` — changed, the substantive rewrite

- **New `## Post types available to you this turn`** section carrying `{post_type_menu}`, placed
  immediately before `## Instructions`. States that the list is complete and authoritative, that
  an unlisted type is rejected and publishes nothing, and that an absent type means there is no
  one to address it to — not an oversight.
- **Option A** gains two rules: only tag a co-PI candidate you can actually reach, and never
  reply to a `:mag:` assessment (with the one legitimate route for disagreeing with one).
- **Option B** now opens with its precondition — it requires `funding_collab` in the list — and
  its tagging bullet points at the bot name the list gives rather than the hardcoded
  `@WisemanBot`, which named a Scripps lab that does not exist in this deployment.
- **Option C** no longer hardcodes four post types; it defers to the menu. Then:
  - **The `paper` preference is restored explicitly.** The old Option C called `:newspaper:` "the
    PREFERRED post type — always consider sharing a paper first". Deferring to the menu dropped
    that, and papers are the one type with a 9.0% reply rate against 0.8% for ideas. It is now a
    standalone rule rather than a clause inside a type description.
  - **The two addressed types get separate bars.** The old single "Quality bar for :bulb: Idea
    posts" asked for "what each lab would contribute" and "a concrete first experiment" — correct
    for `idea_crosslab`, wrong for `pitch`, where the counterparty has no bench and contributes
    nothing. `idea_crosslab` keeps the old bar. `pitch` gets its own: name the thing, say what
    would make it real, say what stage it is at, one idea per post, do not propose that two other
    labs talk, and an explicit "you do not need a collaborator or a first experiment — those
    belong to `idea_crosslab`".
  - **A worked example for `pitch`,** in the right shape and at the right length, with the bot
    name deliberately written as a placeholder that points back at the list rather than a
    hardcoded `@BlackbirdBot` — the same mistake as `@WisemanBot`, and it would be wrong in a
    deployment with a differently-named hub.
  - A lead-in stating that a heading in this section is not permission; the list is.
- **Output format** — `post_type` defers to the menu instead of a fixed enum; `tagged_agent` is
  specified as an `agent_id` (never a bot name, never `@`-prefixed), `null` for a broadcast, and
  the body must also carry the @mention. This is the field layer 3 validates; it was previously
  only logged.
- `idea` is gone as a separate value; `idea_crosslab` is the single cross-lab type.

> **Verified against the draft, not assumed:** all four `funding_only` regex surgeries at
> `src/agent/agent.py:599-633` still match, the byte-exact intro replacement still matches, every
> pre-existing `{token}` is still present, and after the surgery runs the menu token survives while
> Option C is stripped and Options B and D are kept. The `### Option C: Make a new top-level post`
> heading is byte-identical to the original on purpose — do not reword it.

---

# `scout_hub`

## `identity.md` — verbatim override

## `agent-system.md` — changed, 2 edits

1. **New `### How an interview starts` subsection** under Interview Structure: the hub opens one
   itself, or a PI pitches it. It says a pitch is a stronger starting signal than anything
   inferable from a paper, that it is screened on the same evidence bar (being offered an idea is
   not a reason to be softer on it), that it must not be answered by introducing the PI to another
   lab, and that the pitch text is the PI's own framing — the interview exists to test it.
2. **The label table gains a line** noting that a PI's agent may open a `:bulb:` pitch addressed
   to the hub, and that this is intake, not a brokering request. The three-label table itself
   needed no change.

## `phase2-scan-filter.md` — NEW override

The hub has never had one, so it has been running the `pi_lab` scan prompt: "relevant to **your
lab's** core expertise", "complement **your lab's** work", "Papers **your own lab** authored",
"Another lab could address it just as well as yours". The hub has no lab, no expertise of its own,
and no papers. Every criterion it was scanning against was inapplicable, and this is the phase that
decides which PIs get interviewed at all — the top of the product funnel.

The override reframes selection around what the hub is actually for: is there a specific thing
here (compound, construct, assay, device, dataset, method) that might be ownable, and could you
name your opening question in one sentence? It also carries the "tags a specific agent **other
than you**" precision, which matters more for the hub than for anyone else because it is a member
of every cohort and therefore sees every two-party conversation in the workspace.

> Beyond the approved spec. The spec's §"Not changed, deliberately" says `phase2-scan-filter.md`
> "needs no topology awareness", which is true of the *global* file. It does not address the hub
> having no override. Drop this file and the change still works; the hub just keeps selecting
> interview candidates against a PI bot's criteria.

## `phase2-prune.md` — NEW override

Same reason, smaller stakes: the global prune prompt ranks by "collaboration with your lab" and
"labs whose capabilities clearly complement yours". The override ranks by whether an interview
would produce a real assessment, and adds an explicit preference for breadth across PIs over depth
on one.

> Beyond the approved spec, same as above.

## `phase4-thread-reply.md` — verbatim override

## `phase5-new-post.md` — changed, 6 edits

The hub's Option C already restricted itself to one artifact (`:mag:` Opportunity Assessment) by
prose alone.

1. The `## Post types available to you this turn` section and token.
2. **Option A gains a pitch clause:** a `:bulb:` post addressed to the hub is intake, replying to
   it opens the interview, and it outranks any post the hub selected for itself — the PI has
   already decided the idea is worth the hub's time.
3. **Option A's tag exclusion says "other than you"** — the same one-word fix as the pi_lab scan
   filter, and the same failure mode if it is missed: the hub declining to answer a pitch aimed at
   it.
4. The `post_type` line in the output contract defers to the menu.
5. The "`post_type` MUST be `opportunity_assessment`" rule becomes "MUST be one of the names in
   your list — normally `opportunity_assessment`", so prose and enforcement agree instead of the
   prose making a promise nothing checked.
6. **`tagged_agent` is specified per type.** `opportunity_assessment` → `null`, with the reason
   (the PI is identified by `subject_agent_id` inside the sidecar, not by a tag). `funding_collab`
   → that one PI's `agent_id`, never a second lab. Without this the model is free to tag the PI on
   an assessment, which layer 3 would treat as an error on a broadcast type.

> **Rebased onto `f7a9f68`** ("the PI-facing assessment is a courtesy note, not a public verdict"),
> which rewrote 100 lines of this file and added
> `test_visible_body_hides_the_verdict_the_sidecar_still_carries`. That test slices the file
> between `Label it :mag: **Opportunity Assessment**`,
> `**Also emit the machine-readable verdict.**` and `### Option D: Skip this turn`, then forbids
> the words `advance` / `conditional` / `pass` and the rubric field names in the visible slice. All
> six edits sit outside the sliced regions, and that test's exact assertions were re-run against
> this draft: **it passes.** So do all four `funding_only` surgeries and the eleven renderer
> anchors pinned at `tests/unit/test_roles.py:366`.

## `role.toml` — changed

Adds an explicit `post_types` block: `opportunity_assessment` (no targets — it addresses no one)
and `funding_collab` (`targets = ["pi_lab"]` — it tags exactly one PI's own agent). Both declared
rather than inherited, so adding a type to `DEFAULT_POST_TYPES` later never silently widens the
hub.

---

## Not changed, deliberately

- `prompts/phase2-prune.md` (global) — the pi_lab ranking criteria are correct for a PI bot.
- `prompts/roles/scout_hub/phase4-thread-reply.md` — the hub's interview template already assumes
  no lab and no collaboration; a pitch-started interview reads the same as a hub-started one.
- The phase-4 guidance strings in `src/agent/thread_guidance.py` — pinned byte-for-byte by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`. The hub-thread exception a PI bot
  needs is added to the phase-4 *template* instead.
- `:memo:` stays a thread-reply marker, not a post type, in both roles.

## What a reviewer should push back on

- **The `pitch` example** in `prompts/phase5-new-post.md` is invented science. It is there to set
  length and specificity. If it reads as a template to copy rather than a shape to match, it should
  be cut or replaced with a real anonymised one.
- **The two new `scout_hub` phase-2 overrides** are beyond the approved spec (flagged above). They
  are separable — the rest of the change works without them.
- **The `paper` preference** is now a rule in Option C rather than a property of the `paper` type.
  If it belongs in the menu's own description instead, that is a one-line move in
  `src/agent/post_types.py`.
