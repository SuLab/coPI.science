# Specialist response truncation: root cause and remediation

**Status:** root causes established for both truncation paths below.
Remediation for Issue A is approved and already in the tree
(`PANEL_NOTE_QUESTION_CHARS` recalibrated, `clip_rate_warning` drift alarm
added). Issue B's ceiling and retry policy are left as-is, by decision. No
further code change is proposed by this document.

**Scope:** truncation of specialist responses in/around Slack posts, audited
2026-08-26 against every `specialist_consults` row (783 consults over 6
runs) plus the live run `89442d15`.

---

## 0. Three truncation layers exist, by design

1. **Total redaction.** The specialist's opinion body, `concerns`,
   `questions_to_ask` and `confidence` NEVER reach Slack. The only
   workspace-visible artifact of a consult is the panel note: `🧪 Panel ·
   {domain} — {signal} — asked: "{question}"`. Enforcement is structural:
   `format_panel_note` (`src/agent/specialists.py:177`) takes exactly three
   publishable values (domain, verdict_signal, question); the engine's
   closure sinks everything else into `**_withheld`
   (`src/agent/simulation.py:4375`, the `_post_panel_note` method, roughly
   `:4365-4486`). Audit: 0 of all `agent_messages` contain a verbatim
   40-char prefix of any consult's first concern; 0 contain the raw-JSON
   marker `"verdict_signal"`. Caveat recorded: substring audits cannot
   detect paraphrase-level leakage — the invariant rests on the signature
   enforcement (the three-argument contract), not on this audit.
2. **The question clip.** `clip_question()` (`src/agent/specialists.py:203`)
   clips the hub's question — the note's only free text — at
   `PANEL_NOTE_QUESTION_CHARS` on a word boundary, with an ellipsis. Issue A,
   below.
3. **API-truncated consults.** Detected via `is_truncated_stop` (refusal OR
   `max_tokens`, `src/services/llm.py:572`, the test itself at `:589`).
   Consequences: (a) the domain is NOT credited to the specialist floor; (b)
   the panel note is CANCELLED — nothing is posted; (c) the durable
   `specialist_consults` row is still written, with `truncated=True`; (d)
   the tool's return string tells the hub the reply was truncated and
   instructs it to "consult {domain} again before you conclude"
   (`src/agent/tools.py:770-774`). Issue B, below.

---

## Issue A — clip calibration drift (cosmetic, live)

- Not purely cosmetic: the question is the one consult field published
  into a workspace-visible interview thread (hub-written text that may
  paraphrase the PI's unpublished claims — the invariant CLAUDE.md notes
  no code checks), so raising 600 -> 850 widens that published slice ~42%
  per clipped note; the parameter was approved with that understanding.
- The 600-char limit was calibrated 2026-08-20 on n=134 questions (median
  398, p90 552, max 814; "95% render complete").
- Rubric v2-era measurement (all consults since 2026-08-24, n=374): p90 605,
  p95 670, p99 811, max 1029. The clip rate had decayed to ~10-15% (run
  `ee419dd3`: 15/229 questions >600; run `89442d15` early sample: 7/47).
- Cross-check: clipped-note counts in `agent_messages` exactly equal
  questions >600 per run (15/15, 11/11, 7/7) — the clip fires precisely as
  designed; only the calibration decayed.
- **ROOT CAUSE:** a static constant enshrining a point-in-time measurement
  of a model-written text distribution that shifts with every rubric/model
  change, with no drift signal.
- **REMEDIATION (approved 2026-08-26):** `PANEL_NOTE_QUESTION_CHARS` 600 ->
  850 (`src/agent/specialists.py:174`) — covers p99 of the current
  distribution. A new pure function `clip_rate_warning(clipped, total)`
  (`src/agent/specialists.py:236`) returns `None` below 20 notes or at
  <=10% clip rate, and a warning string above that; it is wired into the
  engine's panel-note path (`src/agent/simulation.py:4473-4479`) and logged
  once per run (`self._panel_note_clip_warned`). Logs only — no admin UI, by
  decision.

**Operator note:** the WARNING this alarm logs means the calibration has
decayed again, exactly as 600 did. Do not silence it — remeasure
`PANEL_NOTE_QUESTION_CHARS` from `specialist_consults.question` lengths
(same method as this section) and recalibrate to the new p99. Do not guess.

---

## Issue B — consult truncation residual (rare, load-bearing)

- Exactly ONE truncated consult exists since the `truncated` column exists
  (migration `0036`): run `ee419dd3`, domain `legal`, thread
  `1787587019.016339`, 2026-08-24 16:26:42. Its `llm_call_logs.call_stats`
  shows `stop_reason` `"refusal"` at 1,227 output tokens — the API's own
  classifier cut it; the 4,000-token ceiling was irrelevant and has produced
  ZERO truncations since it was set (2026-08-21).
- The ceiling's own history (900 -> 1500 -> 2500 -> 4000) was the same
  static-limit-vs-moving-distribution pattern as Issue A. `call_stats`
  (migration `0032`) closed that feedback loop for tokens and is how 4,000
  was chosen.
- The automatic 2x truncation retry (`src/services/llm.py`) fires only on
  `max_tokens` stops — empirically, the refusal call has a single
  `call_stats` entry, so no retry followed it. Refusals are instead handled
  by the tool's return string instructing the hub to re-consult
  (`src/agent/tools.py:770-774`) — which worked: the hub re-consulted
  `legal` 67 seconds later (16:27:49) and succeeded. The same thread held 3
  successful `legal` consults and exactly 3 `legal` panel notes — the
  truncated one's note was correctly cancelled.
- Full-corpus reconciliation: run `ee419dd3` 229 consults -> 228 notes (the
  -1 is the truncated one); run `6fb83501` 62 -> 62; run `89442d15` 47 -> 47
  (a transient 44-vs-40 snapshot seen during the live run was in-flight
  posting — zero "Panel note skipped"/"Failed to post" lines exist in its
  logs).
- **DECISIONS (2026-08-26, owner-approved):** the ceiling stays 4,000; NO
  automatic refusal retry (the instructed re-consult self-heals and lets
  the hub rephrase, which has better odds against a content-triggered
  classifier than a verbatim retry — observed rate ~1 in 280); per-event
  ERROR logging (already present, `src/agent/tools.py:716-720`) is the
  agreed signal.

### The telemetry gap is closed by documentation, not migration

The durable `truncated` boolean conflates refusal vs ceiling. The split is
fully derivable, and does not need a new column. The derivation recipe:

> Join `specialist_consults` to `llm_call_logs` on `simulation_run_id`,
> with `llm_call_logs.phase = 'consult_' || specialist_consults.domain`,
> matching on channel and `created_at` proximity (consult rows are written
> within ~1s of the call's log row); then read `call_stats` -> per-entry
> `'stop_reason'` (`'end_turn'` = complete, `'refusal'` = classifier cut,
> `'max_tokens'` = ceiling cut; entries are ordered, `kind` `'final'` or
> `'retry'`, and the LAST entry describes the text actually used).
>
> Example that resolved Issue B's one case: `phase='consult_legal'`,
> `created_at` 2026-08-24 16:26:42, `call_stats`
> `[{"seq":1,"kind":"final","stop_reason":"refusal","output_tokens":1227,
> "max_tokens":4000,...}]`.

`phase = f"consult_{domain}"` is written at `src/agent/tools.py:656`; the
join key exists today, nothing needs to be added to reconstruct which cause
truncated any given consult.

---

## Issue C — historical rows (no action)

All 648 pre-`0036` consults have `truncated = NULL`, which the read path
deliberately treats as "not truncated" (documented in CLAUDE.md's `0036`
deploy box). The 3 known-truncated consults on run `8b64a0e0` stay
unmarked, because retroactively guessing would manufacture the very
verification the column exists to assert. Those runs also predate panel
notes entirely (no `phase='panel_note'` rows), so their exposure was the
old reply-prose path, autopsied in
`docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md`. No action
taken or proposed here.

---

## Systemic root cause

Model-facing size limits in this codebase are point-in-time calibrations
with no ongoing signal, so every model/rubric change silently decays them;
the durable fix is a drift signal per limit, not another one-time
recalibration.

---

## What a future operator should do

- If `clip_rate_warning` logs a WARNING: do not raise
  `PANEL_NOTE_QUESTION_CHARS` on intuition. Remeasure
  `specialist_consults.question` lengths for the current rubric/model and
  recalibrate to cover its p99, the same way 600 -> 850 was derived here.
- If a consult truncation is suspected on a specific row: use the
  derivation recipe above (`specialist_consults` join `llm_call_logs` on
  `simulation_run_id` + `phase = 'consult_' || domain` + `created_at`
  proximity) to read `call_stats[].stop_reason` and tell refusal apart from
  `max_tokens` — do not add a new column for this; it is already derivable.
- Do not add an automatic retry on `refusal` stops. The instructed
  re-consult path is the agreed mitigation, and it is already working
  (observed rate ~1 truncated consult in 280).
- Do not backfill `truncated` on pre-`0036` rows. See Issue C.

---

## Provenance

Measured 2026-08-26 against production `specialist_consults` (783 consults
over 6 runs) plus the live run `89442d15`, and against
`src/agent/specialists.py`, `src/agent/simulation.py`, `src/agent/tools.py`
and `src/services/llm.py` as they stand in the tree on that date. No row was
written and no prompt or code was changed in service of this report.
