# Task 27 — the mypy ceiling's number, its provenance, and its slack

## Ruling

**Three rulings, all made by the executor; none was contested by the plan.**

**1. Keep `MYPY_MAX=150`. Do not lower it.** Measured 145, so the slack is 5 — not the 3 the plan
assumed, because Task 19's `79cee44` fixed the annotation defect that had pushed the count to 147.
Options considered:

| option | consequence |
|---|---|
| lower to 145 (zero slack) | any commit that adds one finding is red. ~15 further tasks of this plan still land against these 145; a ceiling with no slack is a ceiling that gets raised in a hurry by whoever it blocks, which is how ratchets rot. Rejected. |
| lower to 146–147 | same failure mode with a fig leaf, and it does not survive a `2.3.x` patch release adding one check (`mypy>=2.3,<2.4` is a range, and `requirements.lock` is runtime-only, so nothing pins the dev extra harder than that). Rejected. |
| **keep 150 (slack 5)** | 3.4 % of 145, matching the proportional headroom `SRC_LINT_MAX` already carries (260 over a measured 251 = 3.6 %). **Chosen.** |
| raise | never. It is a one-way ratchet. |

**2. Do not pin the dev extras (plan Step 3), and do not present pinning as a fix for anything here.**
The 145→147 move was never PyPI and never mypy — see `## Evidence`. The only dev dependency this
ceiling is sensitive to is mypy, and `e8c05b6` already capped it at `mypy>=2.3,<2.4` (#27 I7). A
second, dev-only lockfile would touch `pyproject.toml` and a new lock file, neither of which is in
this task's file list, and would buy protection against a risk the bisect shows has never fired.
Recorded as a deliberate no, not an oversight. The residual — a `2.3.x` patch adding a check — is
what the slack of 5 is for, and the comment says so.

**3. The provenance block is checked for internal consistency, not re-measured.** `test_ci_gate.py`
asserts the block names the measured count, the commit, the mypy version and the ceiling, and that
`measured + slack == ceiling == MYPY_MAX`'s default. It deliberately does **not** re-run mypy and
demand equality with 145: that is #27 I4-e's defect in a new place (a gate that goes red because a
later commit legitimately paid two findings off, or because a patch release found one more).

## Evidence

Every number below is from a **clean `git archive <rev> src pyproject.toml` export**, one venv
(`.venv-test`, mypy 2.3.1, `python_version = "3.11"`), and the exact command `ci.sh` runs. Never the
working tree — measuring the working tree is what produced the wrong comment in the first place, and
this branch's tree carried three other implementers' uncommitted edits throughout this task.

```bash
git archive <rev> src pyproject.toml | tar -x -C "$d"
( cd "$d" && .venv-test/bin/python -m mypy src --ignore-missing-imports > mypy.out 2>&1 )
grep -c ': error:' "$d/mypy.out"
```

| commit | date | mypy findings | delta |
|---|---|---|---|
| `2170efb` | 09-04 10:36 | **147** | two `http_retry.py` `Exception must be derived from BaseException [misc]` |
| `f9541ba` | 09-04 13:44 | **145** | `c1429e2` (10:39) fixed the two `http_retry.py` findings |
| `42f03f4` | 09-04 14:09 | **147** | +2 `profile_pipeline.py` `tuple[Publication \| None, bool]` `[return-value]` |
| `f29e295` | 09-04 14:19 | **147** | — |
| `5090322` | 09-04 14:53 | **147** | — |
| `79cee44` | 09-04 19:15 | **145** | Task 19 fixed the `_insert_publication_tolerating_conflict` annotation |
| `d7ce1a5` | 09-04 19:16 | **145** | — |
| `962aa6c` | 09-04 | **145** | — |
| `9cbfc00` | 09-04 | **145** | the commit the corrected comment cites |

Set-differences were taken on the normalised error lines, so each delta above is the actual two
findings, not an arithmetic coincidence.

**Two corrections to the record.**

- **v1 of the plan said the ceiling "drifts with PyPI". It does not.** Every move it has ever made was
  caused by this repository's own code. `mypy` is version-capped and did not change across the bisect.
- **The plan's own restatement was also incomplete.** It attributed the whole `145 → 147` move to the
  `profile_pipeline.py` annotation. That is right for `42f03f4`, but it does not explain the
  `ci.sh` comment, which claimed 145 **at `2170efb`** — and `2170efb` measures 147 for an unrelated
  reason (`http_retry.py`, fixed three minutes later by `c1429e2`). The 145 in the old comment was
  read off a **working tree that already carried `c1429e2`'s fix**, then labelled with whatever sha
  `git log -1` reported. That is the actual provenance defect, and it is why the corrected block
  leads with "measure a clean export, not the working tree".

Same export of `9cbfc00`, for the record: `ruff check src --output-format=concise --quiet` = **251**
findings against `SRC_LINT_MAX=260`.

**Step 4 / Step 5 — the mypy step's own behavioural pin was vacuous.**
`test_ci_gate.py::test_ci_sh_fails_when_mypy_findings_exceed_the_ceiling` asserted
`"-m pytest" not in proc.stdout`. `ci.sh` echoes the banner `==> pytest (full suite + branch
coverage, ...)` and then runs the command; with no `set -x`, the string `-m pytest` is **never**
printed. Measured on a gate run that really did reach the pytest step (`MYPY_MAX=99999`):

```
gate reached pytest: True
OLD marker  "-m pytest"  present in output: False
NEW marker  "==> pytest" present in output: True
```

So the old assertion held in exactly the state it claimed to forbid. Against the real mutant — the
`exit 1` deleted from `ci.sh`'s mypy-ceiling branch, applied to a `git archive HEAD` export:

```
mutant reached the pytest step: True
OLD assertion  `"-m pytest" not in out`   holds -> True   (mutant SURVIVES)
NEW assertion  `"==> pytest" not in out`  holds -> False  (mutant KILLED)
```

The assertion was also **unreachable**: `subprocess.run(..., timeout=180)` blocks until the child
exits, and a gate that wrongly proceeds runs the ~7-minute suite, so the test died on
`TimeoutExpired` before any assertion executed. Fixed by streaming the child's output and stopping
it at the banner (`run_gate_until_pytest`), which makes the pin an assertion again and kills the
mutant in **23 s** instead of timing out at 180 s.

## Consequence a closing comment must state

For **#27 I1**: the type-check ratchet is in place and its ceiling is now auditable —
**145 measured at `9cbfc00` with mypy 2.3.1, ceiling 150, slack 5** — and the mypy step's own
behavioural pin, which the DoD audit found was red only via `TimeoutExpired`, now fails on a named
assertion. The closing comment must also carry the correction that v1's "the ceiling drifts with
PyPI" diagnosis was **wrong**: the ceiling has only ever moved on this repository's own code, so no
dependency pinning was needed and none was added.
