# Decisions log — `docs/plans/2026-09-04-close-remaining-gaps.md`

Every **DECIDE** verdict in that plan produces a written ruling **in this directory, in the
repository**. v1 of the plan wrote its six rulings into `.superpowers/`, which `.gitignore:122`
ignores (`git ls-files .superpowers` returns 0 files): `sdd-commit` — `git commit --only -- <paths>`
— cannot commit an ignored path, so none of them existed for anyone who came after the session that
made them. Reports and scratch analysis may stay in `.superpowers/`; **decisions and deliverables
may not.**

## The convention

- **One file per task: `task-<N>.md`. Never a shared file.** A single decisions file is a file that
  five parallel groups all write to, which is the exact condition that produced four
  mixed-attribution commits on this branch (`29bee9c`, `5040516`, `eda8603`, `7cc2ea3`). One
  `sdd-commit`'s `flock` cannot prevent it either: it serialises the git index, not two agents
  editing the same file.
- Wherever the plan says "the decisions file", it means **your own task's file**.
- Three headings, in this order:
  - `## Ruling` — the options considered, the option chosen, and why.
  - `## Evidence` — what you measured, with the commands, so the next reader can re-measure.
  - `## Consequence a closing comment must state` — what Task 32/33 must carry into the GitHub
    issue comment or the PR body. If the answer is "nothing", say that explicitly.
- Commit your file with the path list your task prints, and nothing wider.

## Files that are not `task-<N>.md`

- `baseline.md` — Task 1's gate baseline (figures, or what was wrong if it was red). Task 34 compares
  against it and Task 36 quotes it. Task 32 appends `## Closure dispositions` to it.

## Rulings already made by the branch owner

`task-7.md`, `task-8.md` and `task-25.md` were **escalated by the plan and ruled by the branch owner
on 2026-09-04, before their tasks started**. Their `## Ruling` sections are pre-seeded and marked
`RULED BY BRANCH OWNER 2026-09-04`. The implementer of those tasks **implements the ruling and fills
in `## Evidence`; it does not re-decide it.** If you believe a pre-seeded ruling is wrong, stop and
say so in your report — do not quietly implement something else.

## No credential values in this directory

Several of these rulings are about Slack isolation. Record **presence, absence and lengths only** —
never a token, and never a line copied out of `.env`.
