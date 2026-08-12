# Phase 2: Prune Interesting Posts

> **This phase is disabled in code.** The simulation skips it — this prompt is no longer
> issued. It is retained for reference, and in case the guard is ever bypassed: if you are
> reading this in a live turn, follow it exactly as written.

Your "interesting posts" list needs trimming. In this workspace nothing belongs on it —
BlackbirdBot's :mag: Opportunity Assessments are records for Blackbird staff, not posts to
reply to, and your interviews with the hub reach you as threads rather than through this
list.

## Current interesting posts

{interesting_posts}

## Output Format

Return ONLY this JSON — no other text:

```json
{
  "keep_post_ids": []
}
```
