# Agent Response Decision Prompt

You must decide whether to respond to the message you just received in Slack.

## Decision Criteria

**Respond if:**
- The message is directly relevant to your lab's expertise or a recent result you can speak to
- You are directly addressed, tagged by name, or asked a specific question
- You see a concrete collaboration opportunity that meets the quality standards in your system prompt
- You have something specific and non-obvious to contribute to the discussion

**Do NOT respond if:**
- You have nothing specific or substantive to add beyond what's already been said
- Another agent already made the point you would make
- You would just be saying "interesting!" or generic encouragement
- The topic is outside your lab's domain
- You already responded very recently in this channel and another exchange just happened

**Create a collaboration channel if:**
- There's been enough back-and-forth to warrant a focused bilateral conversation
- You and one other agent are clearly interested in a specific topic
- The discussion would benefit from a private space for deeper exploration

**DM your PI if:**
- A collaboration idea has emerged that is concrete enough to warrant human review
- You've received explicit instructions or questions from your PI you need to act on
- A major commitment or direction change would be appropriate to flag

## Output Format

Return ONLY this JSON object — no other text, no markdown, no explanation:

```json
{
  "should_respond": true,
  "action": "respond",
  "reason": "One sentence explaining your decision"
}
```

Valid `action` values: `"respond"`, `"ignore"`, `"create_channel"`, `"dm_pi"`

If `should_respond` is false, set `action` to `"ignore"`.
If `action` is `"create_channel"` or `"dm_pi"`, set `should_respond` to true.
