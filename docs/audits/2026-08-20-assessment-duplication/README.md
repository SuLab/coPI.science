# Assessment duplication and panel-claim audit — 2026-08-20

Findings from monitoring production run `60c53424-eeee-4bb8-8fbb-193ba1463614`
(started 20:48:56Z, stopped gracefully 21:24:28Z, exit 0, 12 turns, **zero
ERROR/CRITICAL/Traceback lines in 951 log lines**).

The run was healthy and every recently shipped feature worked. Four defects were
found anyway, one of them actively corrupting the triage queue while the run was
being watched.

## What was verified working

| Claim | Evidence |
|---|---|
| Deployed image == HEAD | `sha256` over all `src/**/*.py` inside the agent image equalled the same digest over the worktree: `32c80bd6…8279bb` |
| Schema at head | `alembic_version` = `0030`; `ScriptDirectory.get_heads()` = `['0030']` |
| Rubric integrity | banner `version 1.0.0 (content hash b3aea2c8d235)` == `sha256sum prompts/rubric/blackbird-rubric.toml` prefix == `[meta].version` |
| Consults leave a durable trace | `[specialists] … consulted` log lines vs `specialist_consults` rows sampled 4× mid-run: **37/37, 51/51, 54/54** exact |
| Scores are computed, not taken | all 5 rows recomputed exactly via `blackbird_rubric.weighted_score`; every `raw_verdict.weighted_score` was `0` and unused |
| Admin-only redaction (R4) | `raw_opinion` non-null on 42/42 cards for admin, **0/42** for non-admin, column not even SELECTed |

## Finding 1 — three verdicts for one interview (fixed)

Thread `1787151586.955459` (pearce) accumulated **three**
`opportunity_assessments` rows during the run — 2.51, 2.66, 2.69 — roughly one
every five minutes, and it was still climbing when the run was stopped.

**Root cause.** The thread's 12 messages put the hub's replies at ordinals 2, 4,
6, 8, 10, 12. The three rows' `slack_ts` values were the ordinal-8, ordinal-10
and ordinal-12 replies. `phase4_guidance` renders EXPLORE at ordinal ≤ 4, DECIDE
at ≤ 11 and CONCLUDE above — so **two of the three sidecars came from DECIDE
turns**, whose guidance never asks for one. `_capture_hub_assessment` ran on
every phase-4 reply with no phase check, and nothing asked whether the thread
already had a verdict (`grep -c "select(OpportunityAssessment" src/agent/simulation.py`
was `0`).

The enabler is that the `<assessment_json>` contract lives in the *static* body
of `prompts/roles/scout_hub/phase4-thread-reply.md`, so the model sees the full
sidecar spec on every phase-4 turn, not only on the one that asks for it.

Not new: run `88d81cd8` carries huganir ×3, hart ×3, pearce ×2, culotta ×2 and
cai ×2 on single threads, all with the same ordinal-8/10/12 signature.

**Fix.** `_sidecar_refusal` gates both conditions in one place, and a refusal is
recorded on the existing `assessment_drops` surface rather than dropped silently
(the reply is already in Slack by then, so an invisible refusal would be the same
class of failure that table exists to end):

* `premature_sidecar` — the turn was not the interview's CONCLUDE turn.
* `duplicate_thread_verdict` — the thread already holds a verdict.

`_persist_assessment` now returns whether the verdict was **held** (committed, or
queued on `_pending_assessments` for a retry that will still land it). A queued
row counts: letting a second verdict through while the first is still queued
lands both.

`_assessed_threads` is process-local. That is the same scope as every duplicate
observed, and the only durable alternative is a join back through
`agent_messages`, because `opportunity_assessments.slack_ts` is the *reply's* ts
and the table carries no thread id of its own. See "Open follow-ups".

## Finding 2 — `missing_domains` stored JSON `null`, not SQL NULL (fixed)

`OpportunityAssessment.missing_domains` documents three states and spells the
middle one NULL. The column was mapped as a bare `JSONB`, and SQLAlchemy's JSON
type defaults `none_as_null=False`, so Python `None` was persisted as the JSONB
scalar `null`. Measured before the fix: **15 rows** (everything since
2026-08-19) held `jsonb_typeof(missing_domains) = 'null'` with `missing_domains
IS NULL` = false, while **18 older rows** held a true SQL NULL — one logical
state in two physical encodings.

Nothing was mis-rendering. Every consumer reads it in Python (`is None`) or Jinja
(`or []`), and both encodings deserialize to `None`; `grep` confirmed **no
`IS NULL` predicate on any JSONB column anywhere in `src/`**. The exposure was
latent and precise: the first SQL-level reader written against the documented
contract — `WHERE missing_domains IS NULL`, the obvious way to count verified
panels — silently reclassifies every recent verified row as unverified, inverting
the one number the panel instrumentation exists to produce.

**Fix.** `JSONB(none_as_null=True)`, plus data-only migration `0031` normalizing
the 15 rows. `[]` is untouched — it is an array, not the `null` scalar, so the
unverified state stays distinguishable. The regression test asserts at the SQL
level, because through the ORM this bug is invisible.

## Finding 3 — the panel banner claimed a verification that never ran (fixed)

`_panel_state` had three values, and NULL covered two different findings: a real
verification (`advance`/`conditional` held to the floor, no gap found) and a
verdict never held to the floor at all. `PANEL_REQUIRED_FOR` covers only
advance/conditional, so for a `pass` or `route-to-incubation`
`_specialist_floor_gap` returns an empty set before examining a single consult.

Both rendered the green *"Nothing the verdict's own content owed a specialist was
left unconsulted"*. For the second kind that is false on its own terms: the
pearce `route-to-incubation` row has `required_domains_for` naming `clinical`,
and no clinical consult exists on that thread. The janak `pass` is starker —
five content-implied domains, zero consults, same green box.

**Fix.** A fourth state, `not_owed`, with its own neutral banner naming the
exemption and stating plainly that this is not a verification. `not_owed` is the
weakest claim and yields to both `gap` and `unverified`, so a stored finding can
never be unflagged by it; an absent `recommendation` also lands there.
`PANEL_REQUIRED_FOR` moved to `src/agent/specialists.py` so the engine and the
page read one definition.

The exemption itself is deliberate and documented
(`phase4-thread-reply.md:83`) and was **not** changed. Only the claim about it.

The assessments *list* chip needed no change: it renders only when
`panel_incomplete` is true, so it never asserted a verification.

## Finding 4 — `retro_consult_count` counted other interviews (fixed)

`_load_tool_turns` selects `llm_call_logs` by (run, phase, agent, **channel**,
time window) — it cannot filter by thread, because the log table has no thread
id. Several interviews share a channel, so the scan legitimately returns other
threads' turns, which `correlate_turns_to_messages` then returns as `unplaced`.
Summing consult chips over *all* scanned turns attributed those to this
interview: the kevrekidis assessment reported **11 against 7** real consults,
its 4 unplaced turns making up the difference exactly.

Exposure was nil in practice — the number renders only when `consult_count == 0`,
and it was 0 on every row — but it was wrong. Now counted over placed turns only.
Unplaced turns are still shown, under their own heading.

## Finding 5 — the panel never returns `clear` (NOT fixed; out of scope by decision)

The engine's own calibration tripwire fired at shutdown:

```
[specialists] 63 consults this run and NOT ONE returned 'clear'.
A panel that never clears anything cannot discriminate — check persona calibration.
```

Final distribution: **55 caution, 8 blocking, 0 clear.** The tripwire worked as
designed. The persona prompts already carry the instruction it is meant to
enforce — `prompts/specialists/scientific.md`: *"**clear** — nothing in your
domain stands in the way. Say this when it is true; a panel that never clears
anything is noise."* — and the behaviour persists anyway, so this is model
calibration, not a code defect. Tuning eight persona prompts changes screening
output materially and cannot be validated without a measured before/after run.
Left open deliberately.

## Data cleanup

9 duplicate rows deleted (35 → 26), keeping the newest per thread. Backed up
first to `logs/opportunity_assessments_backup_1787265062.sql` (35 column-INSERTs).

"Newest" was verified to be the CONCLUDE verdict on every affected thread, not
assumed:

| thread | verdict ordinals | kept |
|---|---|---|
| huganir `1786745312` | 8, 10, **12** of 12 | 2.77 |
| hart `1787007187` | 8, 10, **12** of 12 | 2.61 |
| cai `1787146874` | 11, **13** of 13 | 2.55 |
| pearce `1787151586` | 8, 10, **12** of 12 | 2.69 |

Post-delete: zero threads carry more than one verdict.

## A latent test defect found on the way

Every test in `TestHubAssessmentRelocation` and
`test_opportunity_assessment_persistence.py` built a `ThreadState(message_count=11)`
intending the CONCLUDE turn. `_reply_to_thread` overwrites `message_count` with
`len(get_thread_history(thread_id))` before computing the phase, and the harnesses
left the `MessageLog` empty — so **every one of those tests was running at ordinal
1, an EXPLORE turn**, and none was exercising the concluding reply it was named
for. Both harnesses now seed real thread history (with `slack_ts` set, or the
reply is kept DB-only and never reaches `client.posted`).

`test_reply_no_sidecar_persists_nothing_and_is_silent_about_it` is now pinned to
`prior_messages=5` — an ordinary DECIDE turn, which is what "every ordinary
interview turn" in its own docstring always meant.

## Open follow-ups (not done here)

1. **`opportunity_assessments` has no `thread_id`.** Every thread-scoped question
   about an assessment is answered by reverse-resolving `slack_ts` through
   `agent_messages` with an `OR` join and a `LIMIT 1`. A real column would make
   the dedup guard durable across a restart and remove the fragile join from
   `assessment_detail._load_thread_messages`.
2. **Other JSONB columns share the `none_as_null` trap** — `scores`, `gating`,
   `red_flags`, `derisking_milestones`, `raw_verdict`, and `specialist_consults`'
   `concerns`/`questions_to_ask`. None documents a NULL contract and none has a
   SQL-level reader today, so none was changed.
3. **Specialist calibration** — finding 5.
4. **`search_prior_art` rate limiting.** Five `429 — treating as unavailable`
   warnings. All verdicts correctly recorded `fto_achievable: "unconfirmed"`
   rather than claiming novelty, which is the honest tri-state, but a screening
   run that never establishes FTO also never requires the `legal` specialist
   (`required_domains_for` adds `legal` only when FTO is `met`).
