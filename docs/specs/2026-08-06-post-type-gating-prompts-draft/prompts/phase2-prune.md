# Phase 2: Prune Interesting Posts

Your "interesting posts" list has grown beyond 20 items. You need to trim it down to
the 20 most promising posts — the ones most likely to lead to a strong collaboration
proposal if you engage with them.

## Current interesting posts

{interesting_posts}

## Pruning Criteria

Keep posts that:
- Have the highest potential for a concrete, specific collaboration with your lab
- Are from labs whose capabilities clearly complement yours (true complementarity)
- Are recent (newer posts generally preferred over older ones)
- Address a gap or need that your lab is uniquely positioned to fill

Remove posts that:
- You've had time to consider and the collaboration angle feels weak or generic
- Are from labs whose work is too similar to yours (parallel, not complementary)
- Are old enough that the conversation opportunity may have passed
- Were initially interesting but, on reflection, wouldn't lead to a specific first experiment

## Output Format

Return ONLY this JSON — no other text:

```json
{
  "keep_post_ids": ["post_id_1", "post_id_2", "...up to 20"]
}
```
