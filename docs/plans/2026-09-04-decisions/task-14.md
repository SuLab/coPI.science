# Task 14 — a lost review race silently discards the PI's rating (R8 / #24 V5)

Plan: `docs/plans/2026-09-04-close-remaining-gaps.md`, Task 14 (DECIDE, then FIX). Evidence:
`verify-web-data-docs.md` Q4.

The `winner is None` branch of `review_proposal`'s `except IntegrityError` arm
(`src/routers/agent_page.py:655-668` at `d9f0797`) logged at ERROR, ran
`record_engagement` + `mark_notification_responded`, committed, and returned a `302` to the
dashboard **byte-identical to the success path**. Nothing the PI typed was persisted — the
rollback three lines above threw the insert away and `winner is None` means no other row
took its place either. The PI saw the normal post-review redirect, and because
`mark_notification_responded` had already flipped their outstanding `EmailNotification` to
`responded`, no reminder chased it.

## Ruling

**(a) Tell the PI, and do not retire the notification** — implemented as

- `raise HTTPException(status_code=409, detail="Your review could not be saved due to a "
  "conflict, please retry") from None`, replacing the 302; and
- `mark_notification_responded` removed from this branch only, so the outstanding
  `EmailNotification` stays `sent` and the reminder loop keeps chasing the proposal.

`record_engagement` stays and is still committed: the PI *did* act, and the only thing that
call does is reset `consecutive_missed`/`last_engagement_at` on the engagement tracker
(`src/services/email_notifications.py:659-670`). Dropping it would count our own write
failure against them and eventually downgrade their e-mail frequency
(`_check_engagement_and_downgrade`, `:572`).

### The options

| | | |
|---|---|---|
| **(a) Tell the PI** | **chosen** | the only option where the PI learns their input was lost, and the only one where a proposal that is still unreviewed keeps a reminder attached to it |
| (b) Keep the 302, stop retiring the notification | rejected | the reminder e-mail would chase it, but the reminder is *at best* the next scheduled digest (`_is_time_to_send`, `:101`) and is suppressed entirely for `email_notification_frequency='off'` or a system-paused mailbox (`check_and_send_notifications:198-204`). The PI is still shown a success page for a write that did not happen, which is the actual defect |
| (c) Leave as built, document it | rejected | the plan admits this is defensible only if (a) is out of scope for #24. It is not: #24's `Fix:` clause names `agent_page.py`'s PI web-message writer — "rollback + one retry then **409**" — as the pattern to mirror, so the 409 is *inside* the clause, not beyond it |

### The mechanism, named before coding (plan Step 4)

The plan says "thread the indicator through the redirect the way the route's other error
paths do". The route's error paths are **not** uniform, so that is not a specification.
There are exactly two families in `src/routers/agent_page.py`:

1. **`raise HTTPException(status_code=<4xx>, detail=…)`.** Six of them in `review_proposal`
   alone (`:510`, `:515`, `:522`, `:524`, `:542`, and the terminal branch of *this same*
   `except IntegrityError` arm, `raise HTTPException(400, "Already reviewed") from None`,
   now `:714`). The 409 variant is `post_agent_message:1461-1466`, reached after a
   rollback-and-retry on `IntegrityError`, which is the precedent **issue #24's own `Fix:`
   clause names**.
2. **`RedirectResponse(url=…/dashboard?slack_error=…)` / `?delegate_error=…`** — `:1843`,
   `:1893`, `:1931`, `:2034`, read back at `:221`/`:379` and rendered by
   `templates/agent/dashboard.html`. (All line numbers post-commit.)

**Family 1 was copied**, specifically `post_agent_message`'s 409. Family 2 was rejected on a
measurement, not on taste: neither of its two parameters is rendered on the surface this
route serves.

- `slack_error` renders at `dashboard.html:314-316`, inside `{% if agent.slack_user_id %} …
  {% elif slack_error %}` **and** inside `{% if agent.status == 'active' %}` (`:279`…`:411`).
  It is therefore invisible to any PI who has already connected Slack, and to every
  `inactive` agent.
- `delegate_error` renders at `dashboard.html:396-398`, inside that same
  `agent.status == 'active'` block **and** inside `{% if is_owner %}` (`:330`…`:409`). It is
  invisible to delegates.

`review_proposal` deliberately serves both of those populations: it admits
`agent.status in ("active", "inactive")` (`agent_page.py:514`, and its docstring says why),
and it is reachable by a delegate (`get_agent_with_access` returns `is_owner=False`). So the
redirect carriers drop the message for exactly the users this branch can strand. A third
parameter plus a new template block would fix that — but `templates/agent/dashboard.html` is
owned by **Task 16** in this plan's File Structure table, and the plan's requirement is that
the indicator actually render, not that a query parameter exist. Family 1 needs no template,
no new context key, and no flash/session dependency (the app has none).

## Evidence

### The notification's retirement is safe to skip — verified against `email_notifications.py`

Read-only; the file is Task 15/17's. Leaving the row `status='sent'` restores exactly the
state that would have existed had the request never been made, and every consumer already
handles it:

- `_process_user_notifications` (`:247`) finds the outstanding row (`:266-273`). Inside the
  reply window it returns `False` **without sending** (`:274-282`) — so **no duplicate
  send**. Past `settings.email_notification_expiry_days` it writes `status='expired'`
  (`:289`) and falls through, and `send_proposal_notification` **reconciles that same row**
  rather than inserting (`:550-565`, against `uq_email_notification_user_thread_category`) —
  so **no stuck sweep and no constraint collision**. That is Task 21.11/21.12's upsert doing
  its job; this branch does not need to do anything extra.
- `_get_unreviewed_proposals_for_user` (`:133`) selects on `ProposalReview.rating != -1`
  existing, not on notification state — so the proposal is still correctly "unreviewed",
  which is the truth after this branch runs.
- `mark_notification_responded` (`:672`) filters `status == "sent"`, so a later successful
  review still retires the row; nothing is left permanently un-retirable.
- The e-mail reply seam keys on `EmailNotification.reply_token`, which is untouched, so a
  PI replying to the already-delivered e-mail can still file the review that way.

The one behaviour that genuinely changes: a PI whose `email_notification_frequency` is `'off'`,
or whose mail is system-paused, gets no reminder at all. They still get the 409 in their
browser, which is why (a) beats (b).

### Commands

```bash
# baselines, on a clean export (other implementers have uncommitted edits in src/)
git archive HEAD | tar -x -C <scratch>/export
(cd <scratch>/export && ruff check src --output-format=concise --quiet | grep -c .)   # 251
(cd <scratch>/export && mypy src --ignore-missing-imports | grep -c ': error:')       # 145

# red-first: the extended test against the pre-fix export
cp tests/integration/test_proposal_review.py <scratch>/export/tests/integration/
(cd <scratch>/export && .venv-test/bin/python -m pytest \
   tests/integration/test_proposal_review.py -q -p no:cacheprovider \
   -k recovery_with_no_winning_row)
```

### Red first, both halves, against `HEAD` (`d9f0797`) exported with `git archive`

Nothing in `src/` was mutated to produce either failure; the pre-fix source is the export.

**Half 1 — the response code.** The extended
`test_review_proposal_recovery_with_no_winning_row_returns_a_clean_response` run against the
export:

```
>       with pytest.raises(HTTPException) as raised:
E       Failed: DID NOT RAISE HTTPException
tests/integration/test_proposal_review.py:687: Failed
------------------------------ Captured log call -------------------------------
ERROR    src.routers.agent_page:agent_page.py:661 IntegrityError on proposal
4aefdc8e-… review write but no winning row was found on re-select -- unexpected
```

The captured log is the proof it failed for the right reason: the branch under test really
did execute and really did return the 302.

**Half 2 — the notification.** The `pytest.raises` block was replaced, **in the scratch
export only**, with a `try/except HTTPException: pass` so the assertions after it could run
against the old behaviour:

```
E       AssertionError: the outstanding reminder was retired for a review that was never
        persisted (status='responded') -- no e-mail will ever chase this proposal again
E       assert 'responded' == 'sent'
```

**Green after, in the working tree:** `143 passed` across
`tests/integration/test_proposal_review.py`, `tests/integration/test_agent_page.py` and
`tests/unit/test_concurrent_write_guards.py` — the same figure Task 13 left. Neighbours
`tests/integration/test_concurrent_inserts.py`,
`tests/integration/test_email_inbound_reply_paths.py` and
`tests/integration/test_email_notification_sweeps_resilience.py` → `28 passed`.

### Ratchets, on a clean export with only this task's two files substituted

`ruff check src` **251** = baseline 251; `ruff check tests` **0**;
`mypy src --ignore-missing-imports` **145** = baseline 145. The `from None` on the new raise
is what keeps `B904` from adding a 252nd finding.

### D33

AST string constants in `src/routers/agent_page.py`, before (346) vs after (344), differ in
five places and no others: the removed `'/agent/'` and `'/dashboard'` f-string fragments and
the removed `'review'` `response_type` argument (all three from the deleted redirect and
`mark_notification_responded` call); one changed `logger.error` format string; one added
`HTTPException` `detail`. No model-facing string changed; `prompts/` untouched.

## Consequence a closing comment must state

`#24`'s closing comment must say that V5's recovery arm in `review_proposal` now ends in a
**409**, not a 302, when the `IntegrityError` was not the review-uniqueness conflict — this
is a **user-visible response-code change** on `POST /agent/{agent_id}/proposals/{id}/review`
for that one branch, and `a5666b4`'s test was amended rather than deleted to record why. The
matching `EmailNotification` is deliberately left `sent`, so the reminder loop continues to
chase a proposal whose review was not persisted.

There is **no** matching change in `reopen_proposal`'s sibling arm: its recovery is real (it
re-creates the PI's inbox guidance row on the Slack-off path, `d9f0797`), so its 302 is
truthful. Task 14 deliberately did not touch it.
