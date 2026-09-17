# D1 / D2 — the rating=0 marker

**Found by:** `audit-impl-web.md` D1 and D2, auditing this branch's own Tasks 16 and 17.
**Ruled:** 2026-09-05, on measurements taken read-only from `copi_verify`.

## Ruling

**D2 — one site, not five.** Three shipped behaviours have to fit together, and they do:

1. the reminder **sweep chases** a reopened proposal — it *is* outstanding, the PI's
   attention is needed (Task 17's `test_a_reopened_proposal_is_still_unreviewed_for_the_reminder_sweep`);
2. the **web form is not re-offered** while the proposal is being refined (a pre-existing
   assertion in `test_reopen_opens_the_private_channel_and_files_the_review_together`
   says so: *"the proposal is still rateable after being reopened for refinement"* is its
   FAILURE message);
3. therefore the **e-mail reply is the PI's path** to rating it.

Only (3) was broken. `email_inbound.py`'s `_handle_review` read the `rating=0` sentinel as
"already reviewed" and returned without writing, while `_send_review_confirmation` still
replied *"Got it — you rated it 4"*. An audit measured **11 of 26 notifiable users** in
that loop: reminded, answered politely, never recorded.

So the fix is that path alone — `rating not in REVIEW_MARKER_RATINGS` — plus the
in-place upgrade the `-1` marker already had. **Tasks 16 and 17 are otherwise correct and
were left exactly as they shipped.** I briefly reverted them and that was wrong: it
removed the reminder instead of accepting the reply, which contradicts (1).

**D1 — discriminate the label.** Measured on `copi_verify`, read-only:

    rating=0 rows                                     233
      with a '[Reopened]%' comment                      6
      with reviewed_by_user_id set                      6   (the same 6)
      with no comment and no reviewer                 227
      clustered 2026-04-30 / 2026-05-01           104 + 123 = 227
    all 233 are submitted_via='web'          -> cannot discriminate on that
    thread_decisions.refined_in_channel set     3 of 1017  -> nor on that

227 are a two-day bulk backfill no reopen writer produces, so Task 16's blanket
"Reopened with guidance" asserted an event that never happened for 227 rows — and the
`Rating: 0/4` it replaced asserted a score the PI never gave (89 times on one PI's own
dashboard). `templates/agent/dashboard.html` now says **"Reopened with guidance"** only
for a row whose comment starts `[Reopened]`, and **"No score recorded"** otherwise.

## Evidence

`REVIEW_MARKER_RATINGS = (-1, 0)` now lives beside `ProposalReview`
(`src/models/agent_registry.py`) with the whole argument in its docstring, so the next
reader does not have to rediscover which question a given predicate is asking.

Red-first: the reply-path test fails against pre-fix source with
`assert 0 == 4 ... the PI's rating was discarded`; the two dashboard-label tests fail with
`a backfill row with no comment and no reviewer was labelled as a reopen`.

## Consequence a closing comment must state

- **#21 V4-3/V4-4 and #20 blocker 5** close as implemented. This is a follow-on fix to
  Task 17's sweep change, not a reversal of it.
- **A PI rates a reopened proposal by replying to the reminder e-mail, not on the web
  dashboard.** That is deliberate and now pinned from both ends.
- **227 legacy rating=0 rows** read "No score recorded" rather than claiming to be
  reopens. They are still chased by the sweep, so a PI who replies with a rating now
  records one. If the branch owner would rather they were suppressed entirely, that is a
  data decision and a follow-up.
