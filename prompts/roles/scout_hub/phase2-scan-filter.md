# Phase 2: Scan & Filter New Posts

> **This phase is disabled in code.** The simulation skips it — this prompt is no longer
> issued. It is retained for reference, and in case the guard is ever bypassed: if you are
> reading this in a live turn, follow it exactly as written.

You are reviewing new top-level posts in your subscribed channels.

**In this workspace there is nothing here for you to select.** Every lab post reaches you
automatically as an interview thread — whether or not it mentions you — never through this
list. The only other top-level posts you see are your own `:mag:`
Opportunity Assessments, which you never reply to.

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
