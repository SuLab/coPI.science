# Gate baseline for `docs/plans/2026-09-04-close-remaining-gaps.md`

Owned by **Task 1**, which runs `MIGCHECK_PORT=55433 ./scripts/ci.sh` in the foreground at HEAD and
records the figures below. **Task 34** compares its final run against them and **Task 36** quotes
them in the PR body. The skeleton was created by Task 2 (the task that lands this directory); the
figures below are **Task 1's measurement**, transcribed into it.

Why it exists at all: v1 of the plan quoted `ci_baseline.log` as green. That run ends `1 failed` /
`GATE_EXIT=1`, one minute before `a47b6c5` fixed the failure, and no full gate had been run since. A
figure nobody can re-find is how that happened, so the figures live in the repo.

## Task 1 — baseline at `64e9981`

**GREEN.** Command, so it is re-runnable:

```bash
MIGCHECK_PORT=55433 ./scripts/ci.sh 2>&1 | tee /tmp/ci_head.log; echo "GATE_EXIT=$?"
```

Figures, verbatim from the gate log:

```
  single head: 0028 (head)
  ruff (src ratchet):   251 findings (ceiling 260)
  mypy (src ratchet):   147 findings (ceiling 150)
  pytest:               2568 passed, 120 skipped in 466.72s (0:07:46)
  branch coverage:      78.80% (floor 60%)
  result:               CI passed, GATE_EXIT=0
  log:                  scratchpad/ci_task1_head.log
```

Against the plan's expectations (Task 1 Step 2), every figure matches:

| figure | expected (plan Task 1 Step 2) | measured at `64e9981` |
|---|---|---|
| commit under test | HEAD | `64e9981` |
| `GATE_EXIT` | 0 | **0**, `CI passed` |
| alembic heads | single head, `0028` | `0028 (head)` |
| ruff, `src/` | 251 / `SRC_LINT_MAX=260` | **251** |
| mypy | 147 / `MYPY_MAX=150` | **147** |
| pytest | 2568 passed / 120 skipped | **2568 passed / 120 skipped** |
| branch coverage | ~78.8 % / `COV_MIN=60` | **78.80 %** |
| wall time | ~8 min | 466.72 s (7:46) |

### How the two ratchet figures were confirmed — and how NOT to measure them

`251`/`147` were independently re-confirmed on a **clean `git archive` export** of
`ea6a1e1` + `554b139`, i.e. the two landed code tasks introduced **no ratchet drift**.

A measurement taken from the **working tree during concurrent execution read 253/148** — three other
implementers' uncommitted edits were in the tree at the time. Those numbers are not a property of any
commit, and anyone who quotes them will conclude a task blew the ceiling when it did not. **The clean
export is the only trustworthy method**; Task 34 and Task 36 must use it rather than a working-tree
run.

## If the baseline was red

*Task 1 Step 3 records what was wrong and what fixed it here.* A red baseline invalidates every
"watch it fail" step in the plan, because a new failure cannot be told apart from a standing one.

## Closure dispositions

*Appended by Task 32, one row per issue: `closes at merge` / `stays open`, and where the remainder is
handled.*
