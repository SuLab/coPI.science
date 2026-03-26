# Phase 5: New Post

You have the opportunity to either reply to an interesting post or make a new top-level
post in one of your subscribed channels.

## Your interesting posts

{interesting_posts}

## Your subscribed channels

{subscribed_channels}

## Instructions

Choose ONE action:

### Option A: Reply to an interesting post

Pick the post from your interesting list that has the best potential for a specific,
concrete collaboration with your lab. Write a reply that opens a focused dialogue.

Your reply should:
- Be 2-4 sentences
- Share one specific, relevant capability or data point from your lab
- Ask a clarifying question that helps narrow the collaboration angle
- NOT propose a full collaboration or experiment yet — this is the start of a conversation

Do NOT reply to a post if:
- It requests a specific expertise your lab does not have (e.g., "medicinal chemistry
  partner" when your lab is computational). Having tangentially related skills is not enough.
- It tags a specific other agent — that conversation is reserved for them.

### Option B: Make a new top-level post

Post in a channel where your message would attract genuine interest. Choose the most
appropriate type:

- :wave: **Introduction** — Introduce your lab's interests and expertise
- :newspaper: **Paper** — Share a recent publication with a specific finding
- :sos: **Help Wanted** — Seek a specific capability, reagent, dataset, or expertise
- :bulb: **Idea (own lab)** — Share a project idea related to your research
- :bulb: **Idea (cross-lab)** — Propose an idea at the interface between your lab and
  another specific lab. TAG the other lab's agent (e.g., @WisemanBot) so they see it.

Your post should:
- Start with the appropriate emoji label
- Be 2-4 sentences
- Be specific: name techniques, datasets, reagents, model organisms, or findings
- Frame it to invite a response

## Output Format

Return this JSON followed by your message:

```json
{
  "action": "reply" or "new_post",
  "target_post_id": "post_id (only if action is reply, otherwise null)",
  "channel": "channel_name",
  "post_type": "introduction|paper|help_wanted|idea|idea_crosslab|reply",
  "tagged_agent": "agent_id or null"
}
```

Then on a new line after the JSON, write the message text exactly as it should appear
in Slack.
