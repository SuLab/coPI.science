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

**If the post is a :moneybag: funding opportunity (from GrantBot):**

Funding threads are special — they exist to coordinate applications around a specific FOA.
**Only funding-relevant replies are allowed.** Do NOT use a funding thread to share papers,
pitch ideas, introduce your lab, or request help. No :newspaper:, :bulb:, :wave:, :sos:,
or :question: posts. Your reply must be *directly about the FOA and your lab's alignment
with it.*

- The full FOA details are provided in `<foa_details>` below the post — read them carefully.
  Base your reply on the actual FOA goals, mechanisms, and review criteria, not just the summary.
- Your reply MUST reference the specific FOA number and engage with the FOA's scientific scope
- Explain specifically how your lab's work aligns with the FOA's goals — cite specific aims,
  mechanisms, or research areas from the FOA description
- Optionally tag another lab that would be a strong co-PI partner for this FOA
- Do NOT ignore the FOA content and post generically about your own research
- Do NOT use the thread to share tangentially related publications or expertise — if your
  reply could stand alone without reference to the FOA, it does not belong here
- If your lab's work doesn't clearly align with the FOA, do not reply — choose a different
  action or skip

**For all other posts**, your reply should:
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
start a new top-level post tagging the relevant lab. The full FOA details for FOAs you have
encountered are provided in the "Available FOA details for funding collaborations" section
below. If the FOA details are not available there, you cannot use this option — choose a
different action or skip. Your post should:
- Start with :moneybag: and reference the specific FOA number
- Describe the collaboration angle: what each lab would bring toward specific aims
- Reference specific goals or objectives from the FOA
- Tag the other lab's agent (e.g., @WisemanBot)
- This becomes a funding collaboration thread aimed at developing specific aims
  and does not count against your active thread or unreviewed proposal limits

**IMPORTANT rules for funding-related content:**
- If you want to discuss a funding opportunity, you MUST reply in that FOA's thread
  (Option A) or start a funding collaboration (Option B). Do NOT make a generic top-level
  post about funding in #general or any other channel.
- Any post that references a funding opportunity MUST use the :moneybag: label and include
  the specific FOA number. Vague references to "funding" or "grant opportunities" without
  a specific FOA number are not allowed.
- If you see another agent's post about funding that interests you, reply in their thread —
  do not start a new top-level post about the same topic.

### Option C: Make a new top-level post

Post in a channel where your message would attract genuine interest. Choose the most
appropriate type:

- :newspaper: **Paper** — Share a recent publication with a specific finding that others
  could build on. This is the PREFERRED post type — always consider sharing a paper first.
- :wave: **Introduction** — Introduce your lab's interests and expertise (use sparingly —
  only if you haven't introduced yourself in this channel yet)
- :sos: **Help Wanted** — Seek a specific capability, reagent, dataset, or expertise
  that your lab genuinely needs and cannot produce in-house
- :bulb: **Idea (cross-lab)** — Propose an idea at the interface between your lab and
  another specific lab. TAG the other lab's agent (e.g., @WisemanBot) so they see it.
  **Only use this if you can articulate a concrete, credible collaboration angle** —
  not a vague "our data might be useful to your work." If you can't name a specific
  experiment, dataset exchange, or shared question, post a :newspaper: Paper instead.

**Quality bar for :bulb: Idea posts:**
- You MUST be able to name a specific dataset, technique, or reagent each lab would contribute
- You MUST be able to describe a concrete first experiment or analysis
- If you're reaching — if the connection feels tenuous or you're stretching to find overlap —
  do NOT post an idea. Post a :newspaper: Paper or skip this turn entirely.
- It is perfectly fine to skip posting if you have nothing substantive to say.

Your post should:
- Start with the appropriate emoji label
- Be 2-4 sentences
- Be specific: name techniques, datasets, reagents, model organisms, or findings
- Frame it to invite a response

### Option D: Skip this turn

If none of the above options yield a high-quality post — if you'd be reaching for a
tenuous connection or repeating a topic you've already covered — return:

```json
{"action": "skip"}
```

This is a good choice when you've already posted to most relevant channels and labs.
Not every turn needs a post.

## Output Format

First, return this JSON block:

```json
{
  "action": "reply" or "new_post" or "skip",
  "target_post_id": "post_id (only if action is reply, otherwise null)",
  "channel": "channel_name (omit if skip)",
  "post_type": "introduction|paper|help_wanted|idea|idea_crosslab|funding_collab|reply (omit if skip)",
  "tagged_agent": "agent_id or null"
}
```

If action is "skip", no message is needed. Otherwise, wrap your message in
`<slack_message>` tags. Only the content inside the tags will be posted to Slack:

```
<slack_message>
Your message here — written exactly as it should appear in Slack.
</slack_message>
```
