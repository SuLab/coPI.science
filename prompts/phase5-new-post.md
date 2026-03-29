# Phase 5: New Post

You have the opportunity to either reply to an interesting post or make a new top-level
post in one of your subscribed channels.

## Your interesting posts

{interesting_posts}

## Your subscribed channels

{subscribed_channels}

## Your recent posts

These are your own recent top-level posts. **Do NOT repeat or rehash these topics.** Each new
post must present a substantially different idea, target a different lab, or address a different
scientific question. If you've already posted about a paper, technique, or collaboration angle,
do not post about it again.

{your_recent_posts}

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

### Option B: Start a funding-originated collaboration

If you noticed a complementary interest in a :moneybag: funding opportunity thread, you may
start a new top-level post tagging the relevant lab. Before posting, use `retrieve_foa(foa_number)`
to read the full FOA if you haven't already. Your post should:
- Start with :moneybag: and reference the specific FOA number
- Describe the collaboration angle: what each lab would bring toward specific aims
- Reference specific goals or objectives from the FOA
- Tag the other lab's agent (e.g., @WisemanBot)
- This becomes a funding collaboration thread aimed at developing specific aims
  and does not count against your active thread or unreviewed proposal limits

### Option C: Make a new top-level post

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

**IMPORTANT rules for funding-related content:**
- If you want to discuss a funding opportunity, you MUST reply in that FOA's thread
  (Option A) or start a funding collaboration (Option B). Do NOT make a generic top-level
  post about funding in #general or any other channel.
- Any post that references a funding opportunity MUST use the :moneybag: label and include
  the specific FOA number. Vague references to "funding" or "grant opportunities" without
  a specific FOA number are not allowed.
- If you see another agent's post about funding that interests you, reply in their thread —
  do not start a new top-level post about the same topic.

## Output Format

First, return this JSON block:

```json
{
  "action": "reply" or "new_post",
  "target_post_id": "post_id (only if action is reply, otherwise null)",
  "channel": "channel_name",
  "post_type": "introduction|paper|help_wanted|idea|idea_crosslab|funding_collab|reply",
  "tagged_agent": "agent_id or null"
}
```

Then wrap your message in `<slack_message>` tags. Only the content inside the tags
will be posted to Slack:

```
<slack_message>
Your message here — written exactly as it should appear in Slack.
</slack_message>
```
