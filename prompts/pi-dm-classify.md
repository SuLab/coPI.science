# PI DM Classification

You are classifying a direct message from a PI (Principal Investigator) to their lab's AI agent.

## PI Message

{pi_message}

## Categories

Classify the message into exactly one category:

- **standing_instruction** — The PI is giving persistent guidance that should shape the bot's future behavior. Examples: "Prioritize aging collaborations", "Don't engage with cryo-EM topics", "Always look for opportunities with the Wiseman lab".
- **feedback** — The PI is commenting on a past action — correcting, praising, or questioning a specific decision the bot made. Examples: "That proposal was too vague", "Good catch on that FOA", "Why did you reply to that post?"
- **question** — The PI is asking for information or a summary. Examples: "What are you currently exploring?", "Summarize the funding opportunities", "What are your standing instructions?"

## Output

Return ONLY this JSON — no other text:

```json
{
  "category": "standing_instruction|feedback|question",
  "implies_standing_instruction": true/false,
  "summary": "one-sentence summary of what the PI wants"
}
```

Notes:
- `implies_standing_instruction` is true if a feedback message implies a persistent rule (e.g., "Why did you reply to that structural biology post? We don't do that" implies "don't engage with structural biology")
- For questions, `implies_standing_instruction` is always false
