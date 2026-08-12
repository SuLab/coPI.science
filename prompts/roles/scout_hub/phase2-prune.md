# Phase 2: Prune Interesting Posts

> **This phase is disabled in code.** The simulation skips it — this prompt is no longer
> issued. It is retained for reference, and in case the guard is ever bypassed: if you are
> reading this in a live turn, follow it exactly as written.

Your "interesting posts" list needs trimming. In this workspace nothing belongs on it: you
do not scout unsolicited posts, and the pitches you interview reach you automatically as
threads rather than through this list.

## Current interesting posts

{interesting_posts}

## Output Format

Return ONLY this JSON — no other text:

```json
{
  "keep_post_ids": []
}
```
