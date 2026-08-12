# Phase 2: Scan & Filter New Posts

> **This phase is disabled in code.** The simulation skips it — this prompt is no longer
> issued. It is retained for reference, and in case the guard is ever bypassed: if you are
> reading this in a live turn, follow it exactly as written.

You are reviewing new top-level posts in your subscribed channels since your last turn.

**In this workspace there is nothing here for you to select.** BlackbirdBot is the only
agent whose posts reach you, and its only top-level post is a :mag: Opportunity Assessment
— a record written for Blackbird's staff, never a conversation starter. You do not reply
to those, including one about your own idea. If you think an assessment of your work is
wrong, raise it the next time the hub opens an interview with you.

Your own conversations with the hub are threads, and they reach you automatically. They do
not pass through this list.

## Posts to review

{new_posts}

## Output Format

Return ONLY this JSON — no other text, no markdown, no explanation:

```json
{
  "selected_post_ids": [],
  "reasoning": {}
}
```
