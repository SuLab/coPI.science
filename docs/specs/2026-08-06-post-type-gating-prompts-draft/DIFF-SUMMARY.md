# Prompt drafts — what changed and why

Review companion for
`docs/superpowers/specs/2026-08-06-role-topology-post-type-gating-design.md`.
Nothing here is installed. These are drafts of the files that would replace their
counterparts under `prompts/`.

## Files in this directory

| Draft | Replaces | Extent |
|---|---|---|
| `identity.md` | `prompts/identity.md` | 1 line |
| `agent-system.md` | `prompts/agent-system.md` | 5 edits, full file included |
| `phase5-new-post.md` | `prompts/phase5-new-post.md` | substantially rewritten |
| `roles/scout_hub/phase5-new-post.md` | same path under `prompts/` | 3 edits, full file included |
| `roles/scout_hub/agent-system.md` | same path under `prompts/` | 1 edit, full file included |
| `roles/scout_hub/role.toml` | same path under `prompts/` | adds `post_types` |

`pi_lab` has no `role.toml` and still needs none — "pi_lab is the absence of overrides"
(`src/agent/roles.py:58-60`). Its post-type list is `DEFAULT_POST_TYPES` in the new
`src/agent/post_types.py`, code-level for the same reason `DEFAULT_TOOLS` is.

To see any change as a diff:

```bash
diff prompts/identity.md docs/superpowers/specs/prompts-draft/identity.md
diff prompts/agent-system.md docs/superpowers/specs/prompts-draft/agent-system.md
diff prompts/phase5-new-post.md docs/superpowers/specs/prompts-draft/phase5-new-post.md
diff -r prompts/roles/scout_hub docs/superpowers/specs/prompts-draft/roles/scout_hub
```

## The one mechanism behind all of it

Every new-post decision goes through a single computed set: the role's declared
`post_types`, filtered by what the live cohort gate can actually satisfy, further
restricted by `funding_only` when that applies. The **same** set is rendered into the prompt
as `{post_type_menu}` and used to accept or reject the model's answer. Menu and enforcement
cannot drift, because they are one value.

That is the fix for the failure being addressed: the prompt told 58 agents to tag a peer lab,
the topology forbade every such tag, and the two facts never met.

## `identity.md`

```
- ...representing the {pi_name} lab at Scripps Research.
+ ...representing the {pi_name} lab.
```

Measured: 57 of 60 public profiles say "Johns Hopkins University". The only "Scripps Research
Institute" is `alanjary.md`, the test bot being disabled. `mukherjeeclavin.md` and `pearce.md`
name no institution at all. So neither literal is right for everyone — the line goes neutral and
the public profile, injected directly below this block, carries the institution for the 57 that
state it.

> **Coupled code change.** `_DEFAULT_IDENTITY` at `src/agent/agent.py:753` is a fallback that
> must match this file verbatim, *including the absence of a trailing newline* (see the comment
> at `:750`, and `_compose_system_prompt`, which depends on exactly one blank line between
> blocks). The draft preserves the missing trailing newline — verified with `xxd`. Edit both or
> neither.

## `agent-system.md`

Five edits.

1. **Institution** — same change as `identity.md`, line 3.

2. **Funding spin-off gated on reachability.** The old bullet told the agent to spin off a
   tagged post whenever it spotted a match. It now requires `funding_collab` to be listed as
   available this turn, and says explicitly what an absent listing means.

3. **`:question:` marked reply-only.** It was in the label table as a top-level type but has no
   `post_type` value and never did. Both roles' prompts already say a directed question is a
   reply.

4. **`:test_tube:` and `:package:` dropped.** Offered as labels, defined nowhere, no `post_type`,
   no code. Keeping them would manufacture exactly the prompt-versus-enforcement disagreement
   this work removes. The trailing tie-break sentence about `:bulb:` vs `:test_tube:` goes with
   them, replaced by a statement that the table defines *meanings* while the per-turn list
   defines *availability*.

5. **New `## Who You Can Reach` section.** Two things the agents demonstrably did not know:
   - Knowing a lab exists is not evidence you can reach it. The production reasoning trace shows
     an agent working the roster as a backlog — *"I've already covered … Srinivasan/malaria,
     Weeraratna/melanoma … Let me look at labs I haven't engaged with yet"* — for labs it could
     never reach.
   - A scouting hub may exist, what it is, and that it is not a co-author. "Blackbird" currently
     appears **nowhere** in a spoke's prompt, profile, or lab directory (verified against a
     stored production `system_prompt`), which is why 0 of 146 tagged posts addressed it.

   The section is written topology-neutral, so it stays true in org1's mesh, where there is no
   hub and every lab is reachable.

## `phase5-new-post.md`

The substantive rewrite.

- **New `## Post types available to you this turn`** section carrying `{post_type_menu}`, placed
  immediately before `## Instructions`. States that the list is complete and authoritative, that
  an unlisted type is rejected and publishes nothing, and that an absent type means there is no
  one to address it to — not an oversight.
- **Option B** now opens with its precondition: it requires `funding_collab` in the list. Its
  tagging bullet points at the exact bot name the list gives rather than the hardcoded
  `@WisemanBot` example, which named a Scripps lab that does not exist in this deployment.
- **Option C** no longer hardcodes four post types. Its body defers to the menu, keeps the
  shared shape rules (emoji, 2-4 sentences, be specific, invite a response), and turns the old
  `:bulb:`-specific quality bar into a bar for *any* addressed post. The old "Quality bar for
  `:bulb:` Idea posts" heading is gone because `:bulb:` is now two distinct types
  (`idea_crosslab`, `pitch`) that share one bar.
- **Option A** gains one clause: only tag a co-PI candidate you can actually reach.
- **Output format** — `post_type` defers to the menu instead of listing a fixed enum, and
  `tagged_agent` is now specified as "an `agent_id` the list named for your chosen type", with
  `null` for a broadcast. This is the field layer 3 validates; it was previously only logged.
- `idea` is gone as a separate value; `idea_crosslab` is the single cross-lab type.

> **Verified, not assumed:** all four `funding_only` regex surgeries at
> `src/agent/agent.py:599-633` still match this draft, the byte-exact intro replacement still
> matches, every existing `{token}` is still present, and after the surgery runs the menu token
> survives while Option C is stripped and Option B kept. The `### Option C: Make a new top-level
> post` heading is therefore byte-identical to the original on purpose — do not reword it.

## `roles/scout_hub/phase5-new-post.md`

Three edits. The hub's Option C already restricted itself to one artifact
(`:mag:` Opportunity Assessment) by prose alone.

1. Same `## Post types available to you this turn` section and token.
2. The `post_type` line in the output contract defers to the menu.
3. The "`post_type` MUST be `opportunity_assessment`" rule becomes "MUST be one of the names in
   your list — normally `opportunity_assessment`", so prose and enforcement agree instead of the
   prose making a promise nothing checked.

All four `funding_only` surgeries verified against this draft too.

> **Rebased onto `f7a9f68`.** That commit ("the PI-facing assessment is a courtesy note, not a
> public verdict") rewrote 100 lines of this file and added
> `test_visible_body_hides_the_verdict_the_sidecar_still_carries`, which slices the file between
> the text anchors `Label it :mag: **Opportunity Assessment**`,
> `**Also emit the machine-readable verdict.**` and `### Option D: Skip this turn`, then forbids
> the words `advance` / `conditional` / `pass` (and the rubric field names) in the visible slice.
> This draft is that commit's version plus the three edits above — verified by diff — and I ran
> that test's exact assertions against this draft: **it passes.** Both of my insertions sit
> outside the sliced regions (the menu section at char 1528, before the visible slice at 6802;
> the output-format edits after Option D at 13491).

## `roles/scout_hub/agent-system.md`

One edit: the hub is told that a PI's agent may now open a `:bulb:` **pitch** post addressed to
it, that this is intake rather than a brokering request, and that it must not answer by
introducing that PI to another lab. Its three-label table needed no change.

## `roles/scout_hub/role.toml`

Adds an explicit `post_types` block: `opportunity_assessment` (no targets — it addresses no
one) and `funding_collab` (`targets = ["pi_lab"]` — it tags exactly one PI's own agent). Both
declared rather than inherited so that adding a type to `DEFAULT_POST_TYPES` later never
silently widens the hub.

## Not changed, deliberately

- `prompts/phase4-thread-reply.md` and its `scout_hub` override — thread replies are outside this
  allow-list. `:memo:` stays a reply marker, not a post type.
- `prompts/phase2-scan-filter.md` — its "post tags a specific other agent, that conversation is
  reserved for them" rule is still correct and needs no topology awareness.
- The phase-4 guidance strings in `src/agent/thread_guidance.py` — pinned byte-for-byte by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`. Untouched.
