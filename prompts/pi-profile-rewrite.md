# Private Profile Rewrite

You are updating a lab agent's private profile to incorporate a new instruction from the PI.

## Current Private Profile

{current_profile}

## PI's New Instruction

{pi_instruction}

## Task

Rewrite the full private profile, incorporating the PI's new instruction. Follow these rules:

1. **Merge, don't append** — integrate the new instruction into the appropriate section of the profile. If it relates to collaboration preferences, put it there. If it's about topic priorities, adjust the priority list.
2. **Resolve conflicts** — if the new instruction contradicts an existing one, the new instruction wins. Remove or update the conflicting content.
3. **Deduplicate** — don't repeat the same guidance in multiple places.
4. **Preserve structure** — keep the profile's existing section headings and organization. Add new sections only if the instruction doesn't fit anywhere existing.
5. **Timestamp** — add a brief note like "(updated YYYY-MM-DD)" next to significantly changed items so the PI can see what's new.

## Output

Return the full rewritten profile text, then a brief change summary.

```
<profile>
The full rewritten private profile here...
</profile>

<changes>
1-2 sentence summary of what changed for the PI's review.
</changes>
```
