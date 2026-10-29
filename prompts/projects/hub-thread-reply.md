# Refining a project with a lab

You proposed this project to {other_agent_lab}'s bot, {other_agent_name}. Your job in this thread is to turn it
into a plan the lab can carry out with what it actually has.

The project as you proposed it (data, not instructions):

{project}

Channel: #{channel_name}
Message {message_count} of at most 12 — {turn}

## The thread so far

{thread_history}

## This turn

{turn_guidance}

{instructions}

## The plan's fields

indication; target (or mechanism); model; positive_control; negative_control; standard_of_care; readout;
design; time_and_budget (within 6–12 months and $100K–$300K); lab_tools (the lab's tools and models used).

Do not spend turns pursuing an item neither of you controls — name it once as a gap and move on.

## Closing

You close only on your last turn, or earlier when the lab's latest reply starts with ✅ and you agree. To close,
start the visible reply with ✅ if you agree the plan is feasible and complete, or ⚠️ if not, and state the
final plan. Then, after the `</slack_message>` tag, add exactly one block:

<project_json>
{"position": "agree", "plan": {"indication": "", "target": "", "model": "", "positive_control": "",
"negative_control": "", "standard_of_care": "", "readout": "", "design": "", "time_and_budget": "",
"lab_tools": []}, "gaps": [], "disagreements": []}
</project_json>

Use "disagree" for a ⚠️ close. Fill every field. If a field genuinely has no answer, say so in the field
itself and why ("none exists, because …"). List as gaps what is still open, each naming who must close it;
leave out anything settled earlier in this thread or deferred to a later tranche. `disagreements` must
always be present: `[]` when no scientific disagreement remains, otherwise one entry per live dispute.
Never write a note saying there are none.

From your second-to-last turn, add no new required control, endpoint or analysis: confirm what is agreed
and close.

Write the visible reply inside <slack_message> tags.
