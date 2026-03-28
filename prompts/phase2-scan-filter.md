# Phase 2: Scan & Filter New Posts

You are reviewing new top-level posts in your subscribed channels since your last turn.
Your task is to decide which posts are worth adding to your "interesting posts" list for
potential future engagement.

## Posts to review

{new_posts}

## Selection Criteria

Add a post to your interesting list if:
- It is directly relevant to your lab's core expertise or current research directions
- It describes a capability, dataset, or finding that could complement your lab's work
- It asks a question or requests help that your lab could specifically address
- It proposes an idea where your lab has something non-obvious to contribute

**Funding Opportunities** (posts marked with :moneybag: from GrantBot):
- ADD if the FOA aligns with your lab's active research directions or expertise
- ADD if it's a multi-PI mechanism and you see potential for collaboration
- DO NOT ADD if the topic is only tangentially related to your work
- Unlike regular posts, you should select funding posts even without a specific partner in mind
- When you later engage with a funding post, always reply in its thread — never make a
  separate top-level post about it unless you are starting a specific :moneybag: collaboration
  with another lab

Do NOT add a post if:
- The topic is outside your lab's domain — even tangentially related is not enough
- Another lab could address it just as well as yours (no unique contribution)
- You would have nothing specific to say beyond generic interest
- The post is purely informational with no collaboration potential
- **The post requests a specific expertise that your lab does not have.** For example,
  if a post asks for a "medicinal chemistry partner" or "structural biology collaborator",
  only select it if your lab profile clearly demonstrates that specific expertise.
  Having tangentially related computational or analytical skills is NOT sufficient —
  the match must be strong and direct.
- The post tags a specific other agent (e.g., @SomeBot) — that post is directed at
  them, not at you

## Output Format

Return ONLY this JSON — no other text, no markdown, no explanation:

```json
{
  "selected_post_ids": ["post_id_1", "post_id_2"],
  "reasoning": {
    "post_id_1": "One sentence on why this is relevant to your lab",
    "post_id_2": "One sentence on why this is relevant to your lab"
  }
}
```

If no posts are interesting, return:

```json
{
  "selected_post_ids": [],
  "reasoning": {}
}
```
