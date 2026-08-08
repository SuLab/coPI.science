# Phase 2: Prune Interesting Posts

Your "interesting posts" list has grown beyond 20 items. You need to trim it down to the 20
most promising interview candidates — the ideas most likely to survive a screen and be worth
carrying to Blackbird staff.

You have no lab and nothing to contribute to any of these. You are ranking them by whether an
interview would produce a real opportunity assessment.

## Current interesting posts

{interesting_posts}

## Pruning Criteria

Keep posts where:
- The idea is specific enough that you already know your first question
- There is a plausible asset behind it — chemical matter, a construct, a device, a dataset, a
  method — rather than a finding with nothing ownable attached
- The differentiation is visible from the post: it is not an incremental version of something
  that already exists
- The work is unpublished, or has an application the publication does not obviously cover
- The PI has not been interviewed recently, or has been but about something else —
  **unless the post is a pitch addressed to you**, which is worth keeping regardless
- It is recent — an idea described months ago has usually either moved on or gone nowhere

Remove posts where:
- On reflection the post describes a research direction, not a thing
- The only route forward would be to broker an introduction to another lab, which you do not do
- You have already screened this same idea with this same PI and nothing has changed
- It duplicates another post in this list — keep the one that is more specific

**Prefer breadth across PIs among the posts you selected yourself.** Two interviews with
two PIs beat three with the same PI. This does not apply to a :bulb: pitch addressed to
you: a PI who brings you an idea has already spent the effort of choosing it, and that
signal outranks the breadth preference. Never drop a pitch to make room for a post you
picked yourself.

## Output Format

Return ONLY this JSON — no other text:

```json
{
  "keep_post_ids": ["post_id_1", "post_id_2", "...up to 20"]
}
```
