# Task 16 — #20 blocker 5 residual: the review count and the `Rating: 0/4` a PI actually sees

**Status: RULED AND IMPLEMENTED by the Task 16 executor, 2026-09-04.** The plan marks Task 16
**FIX**, not DECIDE, but its commit path list includes this file, and one judgement call inside the
fix genuinely needed writing down (see *Ruling 2*).

## What was open

`f9541ba` closed one of the two causes `audit-phase8-functional.md` breakage 1 named — the scope
join. The other survived at HEAD: `src/routers/admin.py:656` and `:852` filtered
`ProposalReview.rating != -1`, while a PI's *reopen with guidance* writes a `ProposalReview` row with
`rating = 0`. So a reopen marker was counted as a review (breakage 1) and rendered as a score
(breakage 2).

## Measurement on the disposable production copy

`copi-prodtest-db` / `copi_verify`, read-only (`SET default_transaction_read_only = on`), 2026-09-04.

| rating | rows |
|---|---|
| 0 (reopen marker) | **233** |
| 1 | 9 |
| 2 | 12 |
| 3 | 830 |
| 4 | 3 |
| −1 (engine marker) | **0** |

1 087 rows total; `rating <> -1` keeps all 1 087, `rating NOT IN (-1, 0)` keeps 854. The audit's 233
is **confirmed**. The audit's "zero `rating=-1` rows" is also confirmed, which is why the shipped
`!= -1` filter changes nothing an operator can see.

Per-agent, scoped exactly as `admin.py`'s `review_counts` scopes it (join `thread_decisions`,
`outcome='proposal'`, agent is a participant); active agents where the two predicates disagree:

| agent | proposals | `!= -1` | `NOT IN (-1,0)` | markers mis-counted |
|---|---|---|---|---|
| wiseman | 135 | 123 | 34 | **89** |
| briney | 54 | 49 | 20 | 29 |
| su | 44 | 44 | 23 | 21 |
| petrascheck | 59 | 57 | 42 | 15 |
| cravatt | 64 | 57 | 46 | 11 |
| lairson | 39 | 37 | 27 | 10 |
| saez | 16 | 15 | 10 | 5 |
| ken / lotz / deniz | 16 / 15 / 19 | 14 / 12 / 16 | 11 / 9 / 13 | 3 each |
| wu | 12 | 10 | 8 | 2 |
| sali | 6 | 1 | 0 | 1 |

The audit's **89 for wiseman is confirmed**. 12 of 53 active agents are affected; 192 of the 233
markers land on a current proposal of an active agent.

The rendering count reproduces exactly. `/admin/discussions?run_id=all` attaches, per `thread_id`,
only the newest `thread_decisions` row (`decision_map[d.thread_id] = d`, `admin.py:603`); reviews on
that surviving decision are what render. Simulating that join on the copy gives **130 × `0/4`, 9 ×
`1/4`, 11 × `2/4`, 704 × `3/4`, 3 × `4/4`** — the audit's rendered figures to the row.

## Ruling 1 — `rating = 0` is **not** a legal user-supplied rating, so excluding every zero is safe

Checked all three writers and the form:

- `templates/agent/dashboard.html:187-192` offers exactly `(1,2,3,4)` as radio values, `required`.
- `src/routers/agent_page.py:509` — `if rating < 1 or rating > 4: raise HTTPException(400)`.
- `src/services/email_inbound.py:383` — `if not rating or rating < 1 or rating > 4:` → not a review.
- Only `agent_page.py:1013/1029/1090` and `email_inbound.py:1070/1081` write `rating=0`, each
  commented `0 = reopened with guidance, not a rating`.

So no discriminator column is needed, and none exists: on the copy **all 233 zeros carry
`submitted_via='web'`**, identical to a real PI rating, so `submitted_via` could not have served.
The rating value *is* the marker signature — which is the convention `src/agent/simulation.py:3510-3512`
already states in prose: "rating=-1 is a dedicated sentinel — never confused with the explicit 1-4
star rating **or the reopen-with-guidance sentinel rating=0**". This task extends that existing
vocabulary to `0`; it does not add a second mechanism.

## Ruling 2 — the discussions template needs no branch, and one admin-visible string is lost

`templates/admin/discussions.html:162` is fed only by `admin.py:656` (the only other render of that
template, `admin.py:548`, passes `threads=[]`). Once `:656` excludes the marker, a `{% if rev.rating
== 0 %}` branch there would be unreachable code, so **the discussions template is left unchanged** —
the plan's Step 4 wording ("make the templates render a reopen marker as what it is") is satisfied on
that page by not rendering it as a review at all, which is exactly how the `-1` marker is already
handled everywhere.

`templates/agent/dashboard.html:255` **does** need the branch, because its feeder —
`src/routers/agent_page.py:255`, a file this task does not own — still admits `rating=0` into the
`reviewed` list. Without the template branch the PI keeps seeing `Rating: 0/4`.

**Accepted cost:** the reopen guidance text (`comment = "[Reopened] …"`) used to render on
`/admin/discussions` under a false `0/4` badge; it now does not render there at all. The guidance is
still durable in `proposal_reviews.comment`, in the private channel's message history, and on the
PI's own dashboard. Surfacing reopens explicitly on the admin page is a follow-up, not a regression
this task introduces — a misleading number is worse than an absent one.

## Consequence for the #20 closing comment

Blocker 5 is now fully closed: the count is scoped (`f9541ba`) **and** marker-free (this task), and
the marker is no longer rendered as a score on the one page a PI reads. State plainly that the count
rescoping was an extra fix to a real production bug (12 of 53 active agents mis-counted, wiseman by
89) and not one of #20's `Fix:` clauses.

## Not fixed here — siblings that still read `rating != -1`

Each is in a file another task owns; none is a rendering defect, and each is listed so the owner can
decide rather than inherit it silently.

- `src/main.py:172` — the nav badge's unreviewed count. Same defect as `admin.py:852`; the badge
  under-reports outstanding proposals by the same 192 markers. **Group E currently has this file
  checked out.**
- `src/services/email_notifications.py:178` — a reopened proposal counts as reviewed, so the PI is
  never re-notified about it. Arguably correct (the PI did act), but it should be a decision.
- `src/services/email_notifications.py:854` + `_status_label` at `:882-889` — a `rating=0` marker
  yields `top = 0`, which falls through the `{4:…,3:…,2:…,1:…}` map to the literal `"reviewed"` in
  the weekly digest. Wrong label, not a `0/4`.
- `src/routers/agent_page.py:255` — leaves the reopened proposal filed under the dashboard's
  "Reviewed Proposals" heading (now correctly badged "Reopened with guidance"). Moving it to its own
  section needs that route.
- `tests/integration/test_proposal_review.py:1085` pins the old behaviour
  (`assert "Rating: 0/4" in page`) and its own message says to flip it when the defect is fixed
  deliberately. It is not this task's file. **It must be updated before the gate can be green.**
