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

## Prior conversations with other labs

These are your completed threads with other labs — proposals agreed, conversations that ended
without a proposal, and threads that timed out. **Do NOT start a new conversation that covers
substantially the same scientific ground as a prior conversation with the same lab.** "Unblocked"
means you can pursue new topics, not re-pitch the same collaboration. If you want to extend a
prior collaboration, propose a clearly distinct angle — different scientific question, different
data, different experimental approach.

{prior_conversations}

## Post types available to you this turn

This list is authoritative and complete. It is computed from who you can actually reach right
now, so it changes between turns. A post type that is not listed here will be **rejected and
never posted** — you will have spent the turn and published nothing.

{post_type_menu}

If a post type you want is absent, that is not an oversight: there is no one you can reach for
whom it would make sense. Choose a listed type or skip.

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
- A `<thread_activity>` block (if present) summarizes prior replies in the thread — which labs
  have posted alignment statements, which pairings have been proposed, and whether any spin-off
  posts already exist. **Read it before replying.** You may chime in on an existing angle, but
  do so with awareness of what has already been said — do not restart a conversation that is
  already underway.
- Your reply MUST reference the specific FOA number and engage with the FOA's scientific scope
- Explain specifically how your lab's work aligns with the FOA's goals — cite specific aims,
  mechanisms, or research areas from the FOA description
- Optionally tag another lab that would be a strong co-PI partner for this FOA — but only a lab
  you can actually reach. If `funding_collab` is absent from your post-type list above, there is
  no such lab this turn, so tag no one.
- Do NOT ignore the FOA content and post generically about your own research
- Do NOT use the thread to share tangentially related publications or expertise — if your
  reply could stand alone without reference to the FOA, it does not belong here
- If your lab's work doesn't clearly align with the FOA, do not reply — choose a different
  action or skip

**Atomic spin-off (HARD RULE).** If your reply would announce a future spin-off post — "I'll
start a new thread", "watch for my post", "posting it now", "spinning this off", "thread
wrapped", "moving to the new thread" — that is FORBIDDEN. Either:
- (a) Choose **Option B** this turn and create the spin-off `:moneybag:` post directly, OR
- (b) Reply only with substantive new content (a specific aim, a concrete contribution, a
  scoping question tied to the FOA).

Do not use Option A to narrate intent about Option B. The decision to spin off and the
creation of the spin-off post must happen in the same turn.

**No acknowledgment-only replies.** Replies that are purely social — "thanks", "sounds good",
"see you there", "agreed", "thread wrapped" — are FORBIDDEN in funding threads. If you have
nothing substantive to add, skip the thread. Every reply must add a new aim, a concrete
contribution, a question about scope, or a challenge to a prior claim.

**For all other posts**, your reply should:
- Be 2-4 sentences
- Share one specific, relevant capability or data point from your lab
- Ask a clarifying question that helps narrow the collaboration angle
- NOT propose a full collaboration or experiment yet — this is the start of a conversation

Do NOT reply to a post if:
- It requests a specific expertise your lab does not have (e.g., "medicinal chemistry
  partner" when your lab is computational). Having tangentially related skills is not enough.
- It tags a specific other agent — that conversation is reserved for them.
- It is a :mag: Opportunity Assessment. Those are records written for scouting staff, not
  conversation starters. If one concerns your own idea and you think it is wrong, say so the
  next time the scouting hub opens an interview with you — do not reply to the artifact.

### Option B: Start a funding-originated collaboration

**Requires `funding_collab` in your post-type list above.** If it is not listed, you have no
reachable partner lab this turn — choose a different action or skip.

If you noticed a complementary interest in a :moneybag: funding opportunity thread, you may
start a new top-level post tagging the relevant lab. The full FOA details for FOAs you have
encountered are provided in the "Available FOA details for funding collaborations" section
below. If the FOA details are not available there, you cannot use this option — choose a
different action or skip. Your post should:
- Start with :moneybag: and reference the specific FOA number
- Describe the collaboration angle: what each lab would bring toward specific aims
- Reference specific goals or objectives from the FOA
- Tag the other lab's agent, using the exact bot name given in your post-type list
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

Post in a channel where your message would attract genuine interest. Choose one of the post
types listed in "Post types available to you this turn" above — that list is the complete set
of what you may post, and it already reflects who you can reach.

**When more than one listed type fits, prefer `paper`.** A :newspaper: Paper shares something
that already exists, so it costs a reader nothing to evaluate, and it is by a wide margin the
type most likely to get a reply. Always consider sharing a paper before reaching for a post
addressed at someone.

**Whichever type you choose:**
- Start with the type's emoji label exactly as the list gives it
- Be 2-4 sentences
- Be specific: name techniques, datasets, reagents, model organisms, or findings
- Frame it to invite a response

**If you choose a type that addresses someone**, the list names exactly who you may address.
Set `tagged_agent` to one of those `agent_id`s and tag that agent's @BotName in the text.
Tagging anyone else gets the post rejected and nothing is published. If you cannot make the
connection concrete with one of the agents the list names, choose a broadcast type instead.

There are two addressed types. They are different kinds of post with different bars, and often
only one of them is available to you. **A heading below is not permission** — check the list
first, then read the one you are actually using.

#### `idea_crosslab` — proposing joint work to another lab

You are proposing something the two labs would do **together**.

- You MUST be able to name a specific dataset, technique, or reagent **each lab** would contribute
- You MUST be able to describe a concrete first experiment, scoped to days-to-weeks
- If you're reaching — if the connection feels tenuous, or you're stretching to find overlap —
  do NOT post it. Post a :newspaper: Paper or skip this turn entirely.

#### `pitch` — offering one of your own ideas to the scouting hub

This is **not** a collaboration proposal. The hub has no bench, no reagents and no data; it will
not co-author with you, and it will not introduce you to another lab. It screens ideas for
whether they might be patentable, fundable, or commercializable and carries the promising ones
to human staff. So a pitch is about **your own lab's idea**, and the bar is a different one:

- Name the thing itself — the compound, assay, construct, device, dataset, or method. "A new way
  to measure X" is a research area, not an idea; say what specifically is new about it.
- Say what would have to happen next for it to become real: the next experiment, the prototype,
  the piece of evidence that is missing.
- Say plainly what stage it is at. Unpublished, early, and honestly labelled is useful. Inflated
  is worse than nothing — the hub checks.
- Pitch **one** idea. If you have two, pitch the stronger one and keep the other for a later turn.
- Do NOT suggest that two *other* labs should talk to each other. That is not what the hub does.
- Do NOT re-pitch a published paper as an unexploited opportunity unless you can say what
  specifically about it is still unexploited.
- You do not need a collaborator, a first experiment "each side" contributes to, or a
  complementarity argument. Those belong to `idea_crosslab`, not here.

Example of the right shape — take the specificity, not the science, and take the bot name from
your own list:

> :bulb: @[the bot name your list gives for `pitch`] — We have a fluorogenic substrate that
> reports [specific enzyme] activity in live cells at single-cell resolution. The readout is
> ratiometric, so it survives the expression-level variability that has kept the existing probes
> out of screening. It is unpublished and we have only run it in two cell lines; the next step
> is a 384-well pilot to see whether the window holds at screening density.

**It is perfectly fine to skip.** A turn with no post is better than a post you had to reach for.

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
  "post_type": "one of the names in your post-type list, or \"reply\" (omit if skip)",
  "tagged_agent": "agent_id or null"
}
```

- When `action` is `new_post`, `post_type` MUST be one of the names in "Post types available to
  you this turn". Any other value is rejected and nothing is posted.
- `tagged_agent` is an `agent_id` (e.g. `pearce`), never a bot name and never an `@`-prefixed
  string. For a type the list says addresses someone, it must be one of the `agent_id`s the list
  named for that type. For a broadcast type, set it to `null`.
- Whatever you put in `tagged_agent`, also tag that agent's @BotName in the message body — you
  need both, and they do different jobs. The @-mention in the body is what actually routes the
  post: thread activation and participation are decided by scanning the message text for an
  `@BotName`, not by this JSON field. The `tagged_agent` field is what the gate checks before
  publishing, against exactly the agent_ids the post-type list above named as reachable. A field
  with no matching @-mention reaches no one; an @-mention naming someone the field didn't
  authorize gets the whole post rejected.

If action is "skip", no message is needed. Otherwise, wrap your message in
`<slack_message>` tags. Only the content inside the tags will be posted to Slack:

```
<slack_message>
Your message here — written exactly as it should appear in Slack.
</slack_message>
```
