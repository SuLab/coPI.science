# Landscape pages remediation — design decisions

**Status.** Decided with the owner on 2026-11-19, in a brainstorming session.

**Related documents.**
- **The findings (R1–R23):** `docs/audits/2026-11-19-landscape-ui/README.md`.
- **The implementation plan:** `docs/plans/2026-11-19-landscape-ui-remediation-plan.md`, revision 3. It holds:
  - the technical contracts (C1–C14);
  - the packages and the tests;
  - three rounds of adversarial audit.

  It must be revised to match this document; where the two disagree, this document wins.

## Why

The Landscape pages went live with the first active epoch on 2026-11-19. They are read by managers and reviewers who decide whether Blackbird should pursue an opportunity found in Johns Hopkins research. The review found that the pages can mislead those readers:
- the visible verdict contradicts the AI's own call;
- claims cannot be checked against sources;
- model formatting shows as raw symbols;
- the page is twice as long as an assessment;
- in many places the pages depart from the Assessments and Projects pages the same people already use.

## The owner's rule

**Where an earlier design in this project answers a question, follow it.** The earlier designs are the Assessments and Projects pages, their specs and their audits. Each decision below says whether it follows that precedent or is a call made where no precedent exists.

## Goals

- A reader can tell, from the card and then the page, what is proposed and what it costs.
- The reader can see what the AI concluded and why, and how far to trust it.
- The reader can check the claims against their sources.
- The Landscape pages look and behave like the Assessments and Projects pages wherever the content allows.
- Every finding R1–R23 is fixed, or explicitly declined below.

## Non-goals

- Switching the Landscape chat on.
- Changing any pipeline prompt.
- Changing the Assessments or Projects pages.
- Changing how intake counts records.

## Anti-goals

These would be failures even if everything else worked:
- Any change that lets a viewer see something today's hiding rules keep from them:
  - blind batches;
  - expert sample items;
  - other people's decisions before you decide.
- A green "sourced" badge on anything that is not a real document or database record.
- A production data write from the web pages.

## Decisions

### The card (R1, R6, R7) — follows precedent (the Assessments and Projects cards)

The owner chose the Assessments-style card (option B), over the spec's short card, after seeing both mockups side by side.

- **Header:**
  - The plain-language headline is the link.
  - The structured line sits under it, then the lab and the issue date.
  - Each structured line is printed once.
- **Chip:** the AI's **recommendation** in colour. A pass shows as the rubric's display word, "decline", never "pass". Route-to-incubation keeps its own chip.
- **Body:** two columns, stacking on a phone:
  - **left:** the full "In one minute", with a "detail →" button;
  - **right:** first, the **cost line**: the bold first line of "The ask", e.g. "$200–300K · 9–12
    months — …", with the `**` removed. Then the key points, grouped as on the assessment card,
    each with its risk.
- **The cost line** follows the Projects card's "Time and budget" (owner, 2026-11-19). It shows on
  every card, including an undecided blind card: a cost is not a verdict.

  This replaces the spec's "first two sentences of the elevator pitch". The spec's card section (§13.1) is amended.
- **Score and band:** one click down, under "Why this score", with BlackbirdBot's score rationale and the note that the band is computed from the dimension scores, not taken from the AI.
- **No rank number on the card.** The batch keeps the pipeline's order (spec D30); it just isn't numbered.
- **Footer:**
  - the red-flag count and the confirmed deal-breaker count;
  - who is assigned;
  - who has decided, shown only once the viewer has decided (the independent-votes rule).
- **Quick decision:** stays open on the card. The reason tags fold under "Reason tags (optional)".
- **Blind batches:** on an undecided blind card, the chip, the counts and "Why this score" (and so score and band) are hidden until the viewer decides. The cost line stays. This hiding is on the card only. The detail page and the chat record keep showing the recommendation, as they do today.
- **Accepted cost:** a 10-card batch is about 8–10 screens long.

### Lists (R18) — follows precedent

- A **Sort** control and a **Lab** filter, with an Apply button, as both sibling lists have.
  - The default order stays the pipeline's (my call; the spec requires it).
  - The other sort choices are score and recommendation.
  - Sorting uses only the values the viewer can see on the card.
- **Lab filter:** an opportunity matches when any of its labs is the chosen lab (my call: Landscape opportunities can have several labs).
- The list header carries:
  - the band legend, as on the Assessments list;
  - count tiles by recommendation;
  - one sentence saying the cut's ideas are grouped into opportunities.

### Detail page (R2–R5, R8–R17, R19, R21, R23)

**Layout — follows precedent.**
- It copies the assessment detail page:
  - a generic page title;
  - the headline as the brief's title;
  - a sticky jump nav with Expand all / Collapse all;
  - a two-column brief, with "Why this score" beside the pitch;
  - 17 px prose at about 68 characters;
  - the same section order and the same open and collapsed defaults;
  - the same evidence-summary explanations;
  - the same verdict labels ("recommendation" and "score and band", shown as separate labelled values);
  - the same review-form guidance.
- Member ideas are numbered "Idea N", and evidence rows link to them.
- Long panel opinions are clamped with "Show full message".

**Markdown — follows precedent.** The pitch, "The ask" and "Rationale" render as formatted text, as interview assessments do. Rows already stored are treated as Markdown: every Landscape assessment was written under the same Markdown instruction, from 2026-10-30. New rows get marked as Markdown. No data is rewritten.

**Approve / Disapprove — follows precedent; reverses plan revision 3.**
- The buttons are removed, as Assessments did on 2026-09-21 (docs/specs/2026-09-21-assessment-queue-and-headline-contract-design.md, D2/D3).
- The route and its stored history stay.
- Any existing mark still shows read-only.

**History — follows precedent.** A read-only view of an earlier assessment of the same opportunity, reached from the History list. It shows the assessment's own sections only, with no forms and no chat. Assessments keep their old verdicts viewable, and the spec promises "linked as history".

**Criteria override — follows precedent.** When the active criteria went into use by an admin override after a failed check, the Verdict says so in one plain line, with no names or reasons. This matches how Assessments warn openly when a score is weaker than it looks.

**Sources — follows precedent.**
- Claims show their sources visibly, not in a hover tooltip.
- Only DOI, PubMed and clinical-trial references become links, as on Projects. Patents stay unlinked, as on Projects.
- Database records appear as readable names.
- References that point to an internal document become that document's title.
- The verified quotes the spec promises appear under each idea.
- A reference that is not a real source (a specialist's note, an internal analysis step, free text) gets a grey "analysis" badge, never a green "sourced" one. Projects show unmatched items the same way, in neutral grey.
- On an expert's own unsubmitted item, those analysis badges carry no label.

**Deal-breakers.**
- Only flags that were raised or reviewed are listed.
- Confirmed and cleared flags look different.
- Raw evidence JSON is admins-only.
- When flags were confirmed but BlackbirdBot recorded no red flags, the page says why.

**The ask.** The budget specialist's view sits beside "The ask", so a cost the panel disputed is visible.

### Pipeline page (R20) — staff only, my calls where no precedent exists

- **Plain sentences instead of raw JSON** for the latest cut.
- **Overridden criteria** read "held, then overridden".
- **Reject-audit rows** carry the item's title.
- **Override and hold reasons** are visible to staff, folded away by default.
- **Completeness column:** labelled "last slice". A received count above the expected count prints as complete. Intake itself is not changed.

### Help (R16, R19) — follows precedent

- The help page, glossary and tutorials are rewritten to match the pages exactly.
- The glossary gains: rank and order, deal-breaker flags, the three badges, evidence grade, structured line and the licence values.
- Landscape help screenshots are captured from the seeded demo instance on the workstation, as the last step before deploy, as for every other help page.

## Constraints

From the code and the project's rules; the details are in plan §2.
- The pages' data passes the presentation allowlist, so every new field is registered.
- Today's hiding rules (blind, expert, staff-only, independent votes) apply to every new field.
- Only CSS classes already compiled are used.
- Templates follow the rendered-page gate and the scanner tests.
- The deploy rebuilds `blackbird-app` and `landscape-worker` only, with no migration and no simulation.
- One full CI run before deploy.
- Nothing is pushed.

## Left to the builder

- Exact copy and wording, within the help-content rules.
- How the card's key points wrap on a phone.
- The per-member quote cap.
- How the "analysis" badge names its source.
- Test structure.
- The order of work within each phase.
