# Specialist Verdict Vocabulary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the specialist panel's unreachable `clear` label with a
stage-relative `adequate` bar extracted from the Blackbird rubric, split read-failure
out of the judgment axis, and retire a clear-rate alarm that is a permanent false
alarm.

**Architecture:** Phase A (Tasks 1–8) touches no stored label value and is
independently reversible: it adds a calibration harness, derives `read_state` in
code, stops the Slack note asserting verdicts nobody produced, rewrites the alarm,
moves the verdict label after the evidence, and adds four nullable columns. Phase B
(Tasks 9–11) changes the model contract: `established`, then the stage bars
propagated from the rubric into the personas via the existing `{rubric}` mechanism,
then the `blocking`/`gap`/`adequate` rename. The rename and the bars ship together —
`adequate` without a bar is a renamed `clear`.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.x (`Mapped`/`mapped_column`), Alembic,
Postgres 15, pytest, TOML rubric document, Anthropic SDK.

**Spec:** `docs/specs/2026-08-28-specialist-verdict-vocabulary-design.md`

## Global Constraints

- **Run `./scripts/ci.sh` before every commit that touches `src/`.** It is the whole
  gate: alembic sanity, an upgrade→downgrade→upgrade round trip, `ruff check` on
  tests (zero findings) plus a ratcheted ceiling on `src/`, then full pytest with a
  branch-coverage floor. This is what the `pre-push` hook runs.
- **Run pytest on the HOST, not in a container:**
  `.venv-test/bin/python -m pytest tests/ -v`. Never run `pip install` against
  `.venv-test` from an sshfs client — it corrupts console-script shebangs.
- **Never run `pytest --snapshot-update`.** The `.ambr` golden masters pin `pi_lab`
  strings. Nothing in this plan should change them; if one moves, stop and
  investigate rather than regenerate.
- **`verdict_signal` is `String(10)`.** `blocking` (8), `gap` (3), `adequate` (8)
  fit. `disqualifying` (13) does not. Do not introduce a label over 10 characters.
- **The three gating keys are structural** and this plan does not touch them:
  `life_sciences_domain`, `credible_science`, `translational_potential`. The rubric
  validator rejects a fourth.
- **`str.replace`, never `str.format`,** when substituting into prompt text. Prompt
  and profile files contain bare curly braces (`agent.py:322` comments this).
- **`gating` values are the tri-state strings** `"met"` / `"not_met"` /
  `"unconfirmed"`, never booleans. Untouched here, listed so nobody "tidies" it.
- **Never widen `format_panel_note`'s signature.** Its narrowness is the enforcement
  that no opinion content reaches a workspace-visible thread. Concern counts go to
  staff surfaces only (spec D7).
- Branch is `blackbird`. Do not commit to `main`.

## File Structure

| file | responsibility | tasks |
|---|---|---|
| `src/agent/specialists.py` | The specialist contract: labels, parsing, read-state, panel-note formatting, and the pure calibration metrics. Already the home for "what counts as a usable specialist signal". | 1, 3, 5, 11 |
| `scripts/panel_calibration_ladder.py` | **New.** The maintained quality-ladder harness. Absorbs and replaces `scripts/diagnose_specialist_calibration.py`. | 2 |
| `src/agent/tools.py` | The consult execution path: computes `read_state`, renders the stage bar into the persona, and builds the string the hub reads. | 3, 6, 10 |
| `src/agent/simulation.py` | Panel-note posting, the durable consult writer, and the run-level alarm call site. | 4, 5, 8 |
| `src/models/specialist_consult.py` | The consult row. | 8 |
| `alembic/versions/0038_*.py` | **New.** Four additive nullable columns. | 8 |
| `src/services/blackbird_rubric.py` | Rubric parsing/validation and rendering. Gains the `[stage_bar.*]` table and a specialist-scoped render. | 10 |
| `prompts/rubric/blackbird-rubric.toml` | The single source of the bars. | 10 |
| `prompts/specialists/*.md` (8 files) | Persona prompts: schema order, `established`, the `{stage_bar}` placeholder, and the labels. | 9, 10, 11 |
| `src/services/thread_panel.py`, `src/services/assessment_detail.py` | Staff read surfaces. | 4, 11 |

---

# PHASE A — no stored label changes

## Task 1: Calibration metrics as pure functions

Construct sensitivity (R) and invariance (S) from
arXiv:2608.24419, computed here rather than in the harness so they are unit-testable
without API calls. Lives in `specialists.py` for the same reason `clear_rate_warning`
does: what counts as a usable panel signal is a property of the specialist contract.

**Files:**
- Modify: `src/agent/specialists.py` (append after `clear_rate_warning`, which ends at `:462`)
- Test: `tests/unit/test_panel_calibration.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `construct_sensitivity(observations: dict[tuple[str, str], str]) -> tuple[int, int]` and `invariance(observations: dict[tuple[str, str], str]) -> tuple[int, int]`, both returning `(matching, total)` so the caller formats the ratio. `observations` is keyed `(tier, domain) -> signal` for sensitivity and `(framing, domain) -> signal` for invariance.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_panel_calibration.py
"""Construct sensitivity and invariance — the pair from arXiv:2608.24419.

They are INDEPENDENT quantities and no scalar summarises both, which is why
these are two functions returning two ratios rather than one score.
"""
import pytest

from src.agent.specialists import construct_sensitivity, invariance


def test_sensitivity_counts_domains_whose_verdict_moved_between_tiers():
    """R = P(verdict changed | the input's real quality changed)."""
    obs = {
        ("WEAK", "legal"): "caution", ("STRONG", "legal"): "caution",
        ("WEAK", "budget"): "blocking", ("STRONG", "budget"): "clear",
    }
    assert construct_sensitivity(obs) == (1, 2)


def test_sensitivity_is_zero_when_nothing_moves():
    obs = {
        ("WEAK", "legal"): "caution", ("STRONG", "legal"): "caution",
    }
    assert construct_sensitivity(obs) == (0, 1)


def test_invariance_counts_domains_that_held_under_a_wording_change():
    """S = P(verdict unchanged | an edit that does not change the construct)."""
    obs = {
        ("PROD", "legal"): "caution", ("NEUTRAL", "legal"): "caution",
        ("PROD", "scientific"): "clear", ("NEUTRAL", "scientific"): "caution",
    }
    assert invariance(obs) == (1, 2)


def test_both_ignore_domains_present_in_only_one_condition():
    """A domain with no pair cannot be compared and must not silently count as
    agreement — that would inflate S and deflate R."""
    obs = {("WEAK", "legal"): "caution", ("STRONG", "budget"): "clear"}
    assert construct_sensitivity(obs) == (0, 0)
    assert invariance(obs) == (0, 0)


@pytest.mark.parametrize("fn", [construct_sensitivity, invariance])
def test_neither_divides_by_zero_on_empty_input(fn):
    assert fn({}) == (0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_panel_calibration.py -v`
Expected: FAIL — `ImportError: cannot import name 'construct_sensitivity'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/agent/specialists.py`:

```python
def _paired(observations: dict[tuple[str, str], str]) -> list[tuple[str, str]]:
    """Every domain observed under exactly two conditions, as (a, b) signal
    pairs. A domain seen under one condition only is DROPPED rather than
    counted: it cannot be compared, and counting it as agreement would inflate
    invariance and deflate sensitivity — the two errors that would make a
    compressed panel look discriminating."""
    by_domain: dict[str, dict[str, str]] = {}
    for (condition, domain), signal in observations.items():
        by_domain.setdefault(domain, {})[condition] = signal
    pairs: list[tuple[str, str]] = []
    for conditions in by_domain.values():
        if len(conditions) != 2:
            continue
        a, b = (conditions[k] for k in sorted(conditions))
        pairs.append((a, b))
    return pairs


def construct_sensitivity(
    observations: dict[tuple[str, str], str],
) -> tuple[int, int]:
    """``(moved, comparable)`` — how often the verdict CHANGED when the input's
    real quality changed. Higher is better. Published LLM judges average 0.319
    (arXiv:2608.24419); this panel measured 0.594 at one quality rung on
    2026-08-28."""
    pairs = _paired(observations)
    return sum(1 for a, b in pairs if a != b), len(pairs)


def invariance(observations: dict[tuple[str, str], str]) -> tuple[int, int]:
    """``(held, comparable)`` — how often the verdict was UNCHANGED under an
    edit that did not change the construct. Higher is better, but it trades
    against sensitivity: a constant judge scores 1.0 here and 0.0 there, which
    is why both are always reported."""
    pairs = _paired(observations)
    return sum(1 for a, b in pairs if a == b), len(pairs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_panel_calibration.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/specialists.py tests/unit/test_panel_calibration.py
git commit -m "feat(specialists): construct-sensitivity and invariance metrics"
```

---

## Task 2: The maintained calibration ladder harness

Replaces the throwaway `scripts/diagnose_specialist_calibration.py` (98 lines, commit
`84fa1aa`), which calls `generate_agent_response` directly and therefore never
exercises the pinned Opus model, `max_tokens=4000`, or truncation handling.

**Files:**
- Create: `scripts/panel_calibration_ladder.py`
- Delete: `scripts/diagnose_specialist_calibration.py`
- Test: `tests/unit/test_calibration_ladder_fixtures.py` (create)

**Interfaces:**
- Consumes: `construct_sensitivity`, `invariance` from Task 1; `_execute_consult_specialist` from `src.agent.tools`.
- Produces: `CELLS: list[tuple[str, str, str]]` of `(tier, framing, domain)`, and `build_cells() -> list[...]`. Task 10 and 11 read `TIERS` to add a document-bar tier.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_calibration_ladder_fixtures.py
"""The ladder's FIXTURES are testable without spending an API call.

The harness itself makes real Opus calls and is deliberately not in ci.sh; what
is pinned here is that its grid is complete and that every persona is exercised
— a ladder missing a domain would silently exempt exactly the domain most
likely to be broken.
"""
from src.agent.specialists import SPECIALIST_DOMAINS
from scripts.panel_calibration_ladder import CONTEXTS, FRAMINGS, build_cells


def test_every_specialist_domain_is_exercised():
    domains = {domain for _, _, domain in build_cells()}
    assert domains == set(SPECIALIST_DOMAINS)


def test_the_grid_is_the_full_cross_product():
    assert len(build_cells()) == len(CONTEXTS) * len(FRAMINGS) * len(SPECIALIST_DOMAINS)


def test_every_framing_supplies_a_question_for_every_domain():
    """A missing question would be silently skipped, which is how a domain
    disappears from a ladder run without anyone noticing."""
    for name, questions in FRAMINGS.items():
        missing = set(SPECIALIST_DOMAINS) - set(questions)
        assert not missing, f"framing {name!r} has no question for {sorted(missing)}"


def test_tiers_are_ordered_weak_to_strong():
    """`construct_sensitivity` compares ADJACENT tiers, so order is load-bearing."""
    assert list(CONTEXTS) == ["WEAK", "MEDIUM", "STRONG"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_calibration_ladder_fixtures.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.panel_calibration_ladder'`

- [ ] **Step 3: Write the harness**

Create `scripts/panel_calibration_ladder.py`. Port the three tier contexts and the
two framing question sets verbatim from
`docs/audits/2026-08-27-consult-persona-calibration/panel_probe.py` (the archived
probe that produced the RCA's numbers — reuse it so results stay comparable), and
keep the three `STRONG_CASES` from the deleted
`scripts/diagnose_specialist_calibration.py` as a fourth, domain-specific tier named
`LEGACY_2026_08_18` so that run's result stays reproducible.

The parts that must be written fresh:

```python
"""Quality-ladder calibration harness for the specialist panel.

Replaces scripts/diagnose_specialist_calibration.py, which called
generate_agent_response directly and so never exercised the production consult
path's pinned model or max_tokens. This calls _execute_consult_specialist with
BOTH persistence callbacks None: it writes no specialist_consults rows and
credits no specialist floor.

NOT in ci.sh — it makes real Opus calls (~48 for the default grid, about 13% of
a one-hour simulation run).

Run it on any persona, rubric or model change. Acceptance criteria are in
docs/specs/2026-08-28-specialist-verdict-vocabulary-design.md section 7.2.

Usage:
    # inside the agent container, so .env and the baked src/ are both present:
    docker compose -f docker-compose.prod.yml --profile agent run --rm --no-deps \
      -v /host/dir:/probe agent python /probe/panel_calibration_ladder.py
    # or list the grid without spending anything:
    .venv-test/bin/python scripts/panel_calibration_ladder.py --dry-run
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.specialists import (  # noqa: E402
    SPECIALIST_DOMAINS, construct_sensitivity, invariance,
)
from src.agent.tools import _execute_consult_specialist  # noqa: E402

_SIGNAL = re.compile(r"signal:\s*([a-z_]+)", re.I)
_CONCURRENCY = 4


def build_cells() -> list[tuple[str, str, str]]:
    """Every (tier, framing, domain) triple. Ordered tier-major so a partial
    run still covers whole tiers, which is what the acceptance criteria are
    stated over."""
    return [
        (tier, framing, domain)
        for tier in CONTEXTS
        for framing in FRAMINGS
        for domain in SPECIALIST_DOMAINS
    ]


async def _one(tier: str, framing: str, domain: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            out = await _execute_consult_specialist(
                domain, FRAMINGS[framing][domain], CONTEXTS[tier],
                agent_id="blackbird", channel=None,
                # Both None is the whole point: no row, no floor credit.
                on_consult=None, on_consult_record=None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"tier": tier, "framing": framing, "domain": domain,
                    "signal": f"ERROR:{type(exc).__name__}", "detail": str(exc)[:300]}
    m = _SIGNAL.search(out or "")
    return {"tier": tier, "framing": framing, "domain": domain,
            "signal": m.group(1).lower() if m else "UNPARSED", "raw": out}


def report(results: list[dict]) -> None:
    """Per-cell counts, then R and S. Both metrics always, never one: a
    constant judge scores perfectly on invariance alone."""
    labels = sorted({r["signal"] for r in results})
    print(f"\n{'tier':<10}{'framing':<10}" + "".join(f"{s:>12}" for s in labels))
    for tier in CONTEXTS:
        for framing in FRAMINGS:
            cell = [r for r in results if r["tier"] == tier and r["framing"] == framing]
            counts = "".join(
                f"{sum(1 for r in cell if r['signal'] == s):>12}" for s in labels
            )
            print(f"{tier:<10}{framing:<10}{counts}")

    tiers = list(CONTEXTS)
    for a, b in zip(tiers, tiers[1:]):
        for framing in FRAMINGS:
            obs = {
                (r["tier"], r["domain"]): r["signal"]
                for r in results
                if r["tier"] in (a, b) and r["framing"] == framing
            }
            moved, total = construct_sensitivity(obs)
            rate = f"{moved / total:.3f}" if total else "n/a"
            print(f"  R {a}->{b} [{framing}]: {moved}/{total} = {rate}")
    for tier in CONTEXTS:
        obs = {
            (r["framing"], r["domain"]): r["signal"]
            for r in results if r["tier"] == tier
        }
        held, total = invariance(obs)
        rate = f"{held / total:.3f}" if total else "n/a"
        print(f"  S {tier}: {held}/{total} = {rate}")

    print("\nper-domain signal by (tier, framing):")
    for domain in SPECIALIST_DOMAINS:
        row = [
            next((r["signal"] for r in results
                  if r["domain"] == domain and r["tier"] == t and r["framing"] == f),
                 "-")
            for t in CONTEXTS for f in FRAMINGS
        ]
        print(f"  {domain:<13}" + "".join(f"{s:<12}" for s in row))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="list the grid and exit without any API call")
    ap.add_argument("--out", default="panel_calibration_results.json")
    args = ap.parse_args()

    cells = build_cells()
    if args.dry_run:
        for tier, framing, domain in cells:
            print(f"{tier:<10}{framing:<10}{domain}")
        print(f"\n{len(cells)} cells; {len(cells)} Opus calls if run for real.")
        return

    sem = asyncio.Semaphore(_CONCURRENCY)
    print(f"issuing {len(cells)} consults (concurrency {_CONCURRENCY})...", flush=True)
    results = list(await asyncio.gather(*(_one(*c, sem) for c in cells)))
    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    report(results)
    print(f"\nfull results -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run test to verify it passes, and exercise the dry run**

```bash
.venv-test/bin/python -m pytest tests/unit/test_calibration_ladder_fixtures.py -v
.venv-test/bin/python scripts/panel_calibration_ladder.py --dry-run | tail -3
```
Expected: 4 tests PASS; dry run prints `48 cells; 48 Opus calls if run for real.`

- [ ] **Step 5: Delete the superseded harness and commit**

```bash
git rm scripts/diagnose_specialist_calibration.py
./scripts/ci.sh
git add scripts/panel_calibration_ladder.py tests/unit/test_calibration_ladder_fixtures.py
git commit -m "feat(specialists): maintained calibration ladder, replacing the throwaway probe"
```

- [ ] **Step 6: Capture the pre-change baseline**

Run the harness for real (container form, per its docstring) and save the output to
`docs/audits/2026-08-27-consult-persona-calibration/ladder-baseline-preB.json`. **Every
acceptance criterion in Phase B is measured against this file.** Do not skip it —
after Task 11 the old vocabulary cannot be re-measured.

```bash
git add docs/audits/2026-08-27-consult-persona-calibration/ladder-baseline-preB.json
git commit -m "test(specialists): pre-change ladder baseline"
```

---

## Task 3: Derive `read_state` in code

Today `caution` means both "I found a weakness" and "we could not read this reply";
`parse_opinion` defaults to it and `_warn_defaulted` exists only to make the
difference greppable in a log. `truncated` is already computed
(`tools.py:714`); the parse-default case is not tracked at all.

**Files:**
- Modify: `src/agent/specialists.py` — `SpecialistOpinion` (`:133-141`), `parse_opinion` (`:280-354`), plus a new `READ_STATES` and `read_state_for`
- Modify: `src/agent/tools.py` — after `truncated` is computed at `:714`
- Test: `tests/unit/test_specialists.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `SpecialistOpinion.signal_was_defaulted: bool`; `READ_STATES: frozenset[str]` = `{"parsed", "defaulted", "truncated"}`; `read_state_for(*, truncated: bool, opinion: SpecialistOpinion) -> str`. Task 4 consumes `read_state` as a keyword argument; Task 8 persists it.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_specialists.py
from src.agent.specialists import READ_STATES, read_state_for  # add to existing imports


def test_a_reply_that_parsed_reports_its_signal_as_read():
    op = parse_opinion(_raw(verdict_signal="blocking"), domain="chemistry")
    assert op.signal_was_defaulted is False
    assert read_state_for(truncated=False, opinion=op) == "parsed"


def test_an_unreadable_reply_is_marked_defaulted_not_merely_cautious():
    """The 15 defaulted rows in production are byte-indistinguishable from
    genuine cautious ones. This is the field that separates them."""
    op = parse_opinion("this is prose, not an object", domain="chemistry")
    assert op.verdict_signal == "caution"
    assert op.signal_was_defaulted is True
    assert read_state_for(truncated=False, opinion=op) == "defaulted"


def test_an_off_contract_signal_also_counts_as_defaulted():
    op = parse_opinion(_raw(verdict_signal="catastrophic"), domain="chemistry")
    assert op.signal_was_defaulted is True


def test_a_defaulted_confidence_alone_is_not_a_read_failure():
    """Only the SIGNAL decides read-state. A reply whose signal parsed but whose
    confidence was off-contract was still read."""
    op = parse_opinion(
        _raw(verdict_signal="blocking", confidence="extremely"), domain="chemistry"
    )
    assert op.verdict_signal == "blocking"
    assert op.signal_was_defaulted is False


def test_truncation_outranks_a_clean_parse():
    """A reply cut off mid-sentence can still parse if the JSON happened to
    close early. Truncation is the stronger statement and must win."""
    op = parse_opinion(_raw(verdict_signal="blocking"), domain="chemistry")
    assert read_state_for(truncated=True, opinion=op) == "truncated"


def test_every_read_state_returned_is_in_the_declared_set():
    for truncated in (True, False):
        for raw in (_raw(verdict_signal="clear"), "prose"):
            op = parse_opinion(raw, domain="legal")
            assert read_state_for(truncated=truncated, opinion=op) in READ_STATES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -k read_state -v`
Expected: FAIL — `ImportError: cannot import name 'READ_STATES'`

- [ ] **Step 3: Write the implementation**

In `src/agent/specialists.py`, add the field to the frozen dataclass:

```python
    # Whether `verdict_signal` above was READ off the reply or DEFAULTED by
    # parse_opinion. Not a judgement about the opportunity — a fact about the
    # reply — which is why it is derived here and never asked of the model. Its
    # consumer is `read_state_for`; before it existed, a defaulted `caution` was
    # byte-indistinguishable from a genuine one and only a WARNING line
    # (`_warn_defaulted`) recorded the difference.
    signal_was_defaulted: bool = False
```

Set it at both default sites in `parse_opinion`. The `not isinstance(data, dict)`
branch (`:328-333`) becomes:

```python
        return SpecialistOpinion(
            domain=domain, verdict_signal=_DEFAULT_SIGNAL, concerns=(),
            questions_to_ask=(), confidence="low", raw=raw,
            signal_was_defaulted=True,
        )
```

and the off-contract-signal branch (`:335-342`) records it in a local that the final
constructor passes through:

```python
    signal = data.get("verdict_signal")
    signal_defaulted = signal not in VERDICT_SIGNALS
    if signal_defaulted:
        _warn_defaulted(
            domain,
            f"the object parsed but its verdict_signal was {signal!r}, not one of "
            f"{sorted(VERDICT_SIGNALS)}",
        )
        signal = _DEFAULT_SIGNAL
```

…and the returned `SpecialistOpinion` at `:347-354` gains
`signal_was_defaulted=signal_defaulted`. **Do not** set it for a defaulted
`confidence`: only the signal decides read-state.

Then add the pure derivation:

```python
#: The three ways a consult's reply can stand relative to being READ. This is a
#: property of the reply, not of the opportunity, and is deliberately never
#: asked of the model — it is computed from what the code already knows.
READ_STATES: frozenset[str] = frozenset({"parsed", "defaulted", "truncated"})


def read_state_for(*, truncated: bool, opinion: SpecialistOpinion) -> str:
    """Which of ``READ_STATES`` this consult is in.

    ``truncated`` outranks a clean parse deliberately: a reply the API cut off
    can still parse when the JSON happens to close early, and "the API stopped
    this mid-sentence" is the stronger statement about how much of the
    specialist's reasoning we actually have.

    Generalises the special case at ``_post_panel_note``, which pulls
    ``truncated`` out of ``**_withheld`` solely to CANCEL the workspace-visible
    note because "no specialist ever said it". That reasoning applies equally to
    a reply that arrived complete and failed to parse — which is not
    ``truncated``, and which therefore posted a note asserting a verdict nobody
    produced.
    """
    if truncated:
        return "truncated"
    return "defaulted" if opinion.signal_was_defaulted else "parsed"
```

In `src/agent/tools.py`, immediately after `truncated` is assigned at `:714`:

```python
    read_state = read_state_for(truncated=truncated, opinion=opinion)
```

and add `read_state_for` to the module's existing import from
`src.agent.specialists`. (`opinion` is already in scope — it is parsed at `:691`,
before the `truncated` computation.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_specialists.py -v
.venv-test/bin/python -m pytest tests/unit/test_consult_accounting.py -v
```
Expected: PASS. `SpecialistOpinion` gained a defaulted field, so existing
positional constructions are unaffected.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/specialists.py src/agent/tools.py tests/unit/test_specialists.py
git commit -m "feat(specialists): derive read_state, splitting read failure from judgement"
```

---

## Task 4: Cancel the panel note whenever the opinion was not read

**Files:**
- Modify: `src/agent/simulation.py` — `_post_panel_note` (`:4370-4381` signature; the `truncated` cancellation branch follows its docstring) and the `record_consult` closure (`:2027-2042`)
- Modify: `src/agent/tools.py` — pass `read_state` into `on_consult_record`
- Test: `tests/integration/test_specialist_consult_capture.py` (append)

**Interfaces:**
- Consumes: `read_state` from Task 3.
- Produces: `_post_panel_note(..., read_state: str | None = None, ...)`. Task 8 persists the same value.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/integration/test_specialist_consult_capture.py
"""A note is a workspace-visible statement in the PI's own interview thread. It
must not assert a verdict that was defaulted rather than read — the same reason
`truncated` already cancels it."""
import pytest


@pytest.mark.parametrize(
    "read_state, expect_note",
    [("parsed", True), ("defaulted", False), ("truncated", False)],
)
@pytest.mark.asyncio
async def test_only_a_read_opinion_reaches_the_thread(
    engine_with_hub, read_state, expect_note,
):
    posted: list[str] = []
    engine_with_hub.transport.post_message = lambda **kw: posted.append(kw["text"])

    await engine_with_hub._post_panel_note(
        "blackbird", channel="#general", thread_ts="1.1",
        domain="chemistry", question="is the route scalable?",
        verdict_signal="caution", read_state=read_state,
    )

    assert bool(posted) is expect_note


@pytest.mark.asyncio
async def test_a_missing_read_state_still_posts(engine_with_hub):
    """None means "this caller predates the field", not "unread". Failing closed
    here would silently stop every note the moment a caller was missed."""
    posted: list[str] = []
    engine_with_hub.transport.post_message = lambda **kw: posted.append(kw["text"])
    await engine_with_hub._post_panel_note(
        "blackbird", channel="#general", thread_ts="1.1",
        domain="chemistry", question="q", verdict_signal="caution",
    )
    assert posted
```

Adapt `engine_with_hub` to whatever fixture the file already uses for an engine with
a hub agent and a fake transport; do not introduce a new fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_specialist_consult_capture.py -k read_opinion -v`
Expected: FAIL — `_post_panel_note() got an unexpected keyword argument 'read_state'`

- [ ] **Step 3: Write the implementation**

In `_post_panel_note`, add the parameter and generalise the cancellation. Keep
`truncated` in the signature: `read_state` may be absent from an older caller, and
the two together fail closed on either signal.

```python
        verdict_signal: str,
        truncated: bool | None = None,
        read_state: str | None = None,
        **_withheld,
```

Replace the `truncated`-only cancellation with:

```python
        # CANCELLED for any opinion we did not actually read. `truncated` was
        # the original special case and its reasoning was right — "no specialist
        # ever said it" — but it covered only an API cut-off. A reply that
        # arrived COMPLETE and failed to parse is not truncated, and posted a
        # workspace-visible "⚠️ caution" for a verdict `parse_opinion` had
        # defaulted. `read_state` is the general predicate; `truncated` is kept
        # beside it so a caller that supplies only one still fails closed.
        if truncated or (read_state is not None and read_state != "parsed"):
            return
```

In the `record_consult` closure (`simulation.py:2027`), nothing changes — it already
forwards `**fields` to both writers. In `tools.py`, add `read_state=read_state` to
the `on_consult_record(...)` call at `:730-750`, beside `truncated=truncated`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_specialist_consult_capture.py -v
.venv-test/bin/python -m pytest tests/unit/test_specialists.py tests/unit/test_consult_accounting.py -v
```
Expected: PASS

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py src/agent/tools.py tests/integration/test_specialist_consult_capture.py
git commit -m "fix(specialists): cancel the panel note for any opinion that was not read"
```

---

## Task 5: Replace the clear-rate floor with a signal-mix report

The alarm asserts *"A panel that clears almost nothing cannot discriminate — check
persona calibration."* The RCA falsified that for seven of eight domains, and the 5%
floor sits above what a correct panel produces on this population, making it a
permanent false alarm.

**Files:**
- Modify: `src/agent/specialists.py` — `MIN_CONSULTS_FOR_CLEAR_RATE`/`MIN_CLEAR_RATE`/`clear_rate_warning` (`:419-462`)
- Modify: `src/agent/simulation.py:1204` (the call site) and `:411` (`_consult_signal_counts`; add a per-domain tally)
- Test: `tests/unit/test_specialists.py` (replace the seven alarm tests at `:803-860`)

**Interfaces:**
- Consumes: nothing.
- Produces: `signal_mix_report(counts: dict[str, int]) -> str | None` and `domain_flatness_warning(per_domain: dict[str, dict[str, int]]) -> list[str]`. `clear_rate_warning` and `MIN_CLEAR_RATE` are **removed**.

- [ ] **Step 1: Write the failing test**

```python
# replaces the seven tests at tests/unit/test_specialists.py:803-860
# ---------------------------------------------------------------
# The clear-rate FLOOR was retired 2026-08-28. It asserted that a low clear
# share meant the panel could not discriminate; a 48-consult positive control
# falsified that (blocking 87.5% -> 0% across a quality ladder, p = 5.1e-07),
# and the floor sat ABOVE the rate a correct panel produces on this population.
# There is no replacement threshold: the optimal operating point for a screen is
# a likelihood ratio, so a fixed floor on the output rate is the wrong shape of
# constraint. See docs/audits/2026-08-27-consult-persona-calibration/.
# ---------------------------------------------------------------
from src.agent.specialists import domain_flatness_warning, signal_mix_report


def test_the_report_states_the_mix_and_never_diagnoses_a_cause():
    msg = signal_mix_report({"caution": 143, "blocking": 16, "clear": 4})
    assert msg is not None
    assert "163" in msg, "the operator needs the denominator"
    assert "clear 4" in msg and "2.5%" in msg
    assert "cannot discriminate" not in msg, "the retired false assertion"
    assert "persona calibration" not in msg, "it pointed at the wrong thing"
    assert "panel_calibration_ladder" in msg, "point at the instrument instead"


def test_the_report_is_quiet_below_its_sample_floor():
    """Unchanged from the retired alarm: at n<50 a mix report is noise."""
    assert signal_mix_report({"caution": 49}) is None
    assert signal_mix_report({"caution": 50}) is not None


def test_the_report_never_divides_by_zero():
    for counts in ({}, None, {"caution": 0}):
        assert signal_mix_report(counts) is None


def test_a_domain_stuck_on_one_label_is_named():
    per_domain = {
        "technologic": {"caution": 141, "blocking": 3},
        "chemistry": {"caution": 55, "blocking": 27},
    }
    warnings = domain_flatness_warning(per_domain)
    assert len(warnings) == 1
    assert "technologic" in warnings[0]
    assert "chemistry" not in warnings[0], "33% blocking is discrimination, not flatness"


def test_flatness_is_reported_as_a_prompt_to_measure_not_a_verdict():
    """`technologic` sits at 97.9% modal share in production and was FULLY
    quality-sensitive in the ladder. A modal-share number must never convict a
    domain on its own — that is the mistake the retired alarm made."""
    warnings = domain_flatness_warning({"legal": {"caution": 74, "blocking": 17}})
    for w in warnings:
        assert "miscalibrat" not in w.lower()
        assert "panel_calibration_ladder" in w


def test_flatness_is_quiet_on_a_small_per_domain_sample():
    assert domain_flatness_warning({"legal": {"caution": 19}}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -k "mix_report or flatness" -v`
Expected: FAIL — `ImportError: cannot import name 'signal_mix_report'`

- [ ] **Step 3: Write the implementation**

Delete `MIN_CLEAR_RATE` and `clear_rate_warning`; keep
`MIN_CONSULTS_FOR_CLEAR_RATE` renamed to `MIN_CONSULTS_FOR_MIX_REPORT` (same value,
50, same reasoning). Add:

```python
#: Smallest per-domain sample the flatness note will speak on.
_MIN_CONSULTS_FOR_FLATNESS = 20

#: Modal share above which a domain is worth MEASURING. Not a verdict: a domain
#: can be legitimately one-sided (`budget` has never blocked in 67 consults and
#: is not broken), and `technologic` sat at 97.9% modal share while being fully
#: quality-sensitive on the ladder.
_FLATNESS_MODAL_SHARE = 0.95


def signal_mix_report(counts: dict[str, int] | None) -> str | None:
    """The run's signal mix, or None below the sample floor.

    Reports, and deliberately does not diagnose. Its predecessor
    (`clear_rate_warning`) asserted that a low `clear` share meant the panel
    could not discriminate and told the operator to check persona calibration.
    A 48-consult positive control falsified both halves: the panel moved from
    87.5% `blocking` on a weak record to 0% on a strong one (Fisher exact
    p = 5.1e-07), and the clear rate turned out to measure the POPULATION, not
    the instrument — `chemistry` is the most informative domain in the panel
    (0.914 bits) and has never cleared once.

    "counted", because the tally excludes TRUNCATED consults: recorded durably,
    deliberately never counted (tools.py — an unread specialist has cleared
    nothing).
    """
    if not counts:
        return None
    total = sum(v for v in counts.values() if isinstance(v, int))
    if total < MIN_CONSULTS_FOR_MIX_REPORT:
        return None
    parts = ", ".join(
        f"{label} {counts.get(label, 0)} ({counts.get(label, 0) / total:.1%})"
        for label in sorted(counts)
    )
    return (
        f"[specialists] signal mix over {total} counted consults this run: "
        f"{parts}. A low share of the top label is EXPECTED for an early-stage "
        f"population and is not evidence of miscalibration. Discrimination is "
        f"measured by scripts/panel_calibration_ladder.py, not by this ratio — "
        f"see docs/audits/2026-08-27-consult-persona-calibration/."
    )


def domain_flatness_warning(
    per_domain: dict[str, dict[str, int]] | None,
) -> list[str]:
    """One line per domain that returned essentially one label all run.

    A PROMPT TO MEASURE, never a verdict — worded so it cannot be read as the
    retired alarm was. `legal` is the domain this exists to surface: 0 of 91
    `clear` all-time and unmoved across every tier of the ladder.
    """
    out: list[str] = []
    for domain in sorted(per_domain or {}):
        counts = per_domain[domain]
        total = sum(v for v in counts.values() if isinstance(v, int))
        if total < _MIN_CONSULTS_FOR_FLATNESS:
            continue
        modal = max(counts.values())
        if modal / total < _FLATNESS_MODAL_SHARE:
            continue
        label = max(counts, key=lambda k: counts[k])
        out.append(
            f"[specialists] {domain} returned {label!r} on {modal} of {total} "
            f"consults ({modal / total:.1%}). A one-sided domain may be correct "
            f"— run scripts/panel_calibration_ladder.py and check whether it "
            f"moves across quality tiers before changing anything."
        )
    return out
```

In `simulation.py`, add a per-domain tally beside `_consult_signal_counts` at `:411`:

```python
        # signal counts per DOMAIN, for domain_flatness_warning. The run-level
        # tally above cannot see a single stuck domain.
        self._consult_signal_counts_by_domain: dict[str, dict[str, int]] = {}
```

populate it in `_note_consult` (which already receives `domain` and `signal`
alongside the run-level increment at `:4738`), and replace the call site at `:1204`:

```python
        mix = signal_mix_report(self._consult_signal_counts)
        if mix:
            logger.info(mix)
        for line in domain_flatness_warning(self._consult_signal_counts_by_domain):
            logger.warning(line)
```

Note the level change: the mix is INFO because it is a report, not a problem.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_specialists.py -v
grep -rn "clear_rate_warning\|MIN_CLEAR_RATE" src/ tests/ || echo "no stale references"
```
Expected: PASS, and no remaining references.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/specialists.py src/agent/simulation.py tests/unit/test_specialists.py
git commit -m "fix(specialists): retire the clear-rate floor for a signal-mix report"
```

---

## Task 6: Move the verdict label after the opinion body in the hub's context

**Files:**
- Modify: `src/agent/tools.py:775`
- Test: `tests/unit/test_consult_accounting.py` (append)

**Interfaces:**
- Consumes: `read_state` from Task 3.
- Produces: no new symbol; the returned string's shape changes.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_consult_accounting.py
"""The label must not precede the body in what the hub reads.

A model reads its context in order, and a verdict word placed ahead of the
evidence is an anchor on the hub's own reasoning: anchoring on a score already
in context reaches Cohen's d = 0.71 and is NOT removable by instruction
(arXiv:2608.25869). Evidence-before-rating is worth +6 to +11 accuracy points
(arXiv:2305.17926).
"""


@pytest.mark.asyncio
async def test_the_hub_reads_the_opinion_before_it_reads_the_label(fake_anthropic):
    fake_anthropic.queue_text(
        '{"verdict_signal": "blocking", "concerns": ["the route is unscalable"],'
        ' "questions_to_ask": [], "confidence": "high"}'
    )
    out = await _execute_consult_specialist(
        "chemistry", "is the route scalable?", "PI: we have one gram.",
        agent_id="blackbird",
    )
    assert out.index("unscalable") < out.index("blocking"), (
        "the label must come after the body"
    )
    assert "read: parsed" in out
```

Use whatever fake-LLM fixture the file already provides; do not add one.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_consult_accounting.py -k reads_the_opinion -v`
Expected: FAIL — the label currently precedes the body.

- [ ] **Step 3: Write the implementation**

Replace `tools.py:775`:

```python
    # Label AFTER the body, deliberately. This used to be
    # f"{spec.title} — signal: {signal}\n\n{raw}", which put a verdict word
    # ahead of the evidence in the hub's context — the worst position for it.
    # Anchoring on a score already in context reaches Cohen's d = 0.71 and is
    # not removable by instruction (arXiv:2608.25869); generating evidence
    # before rating is worth +6 to +11 accuracy points (arXiv:2305.17926).
    # `read: parsed` is stated so the hub can tell a read opinion from one whose
    # signal was defaulted — the same distinction `read_state` draws for the
    # panel note.
    return (
        f"{spec.title}\n\n{opinion.raw}\n\n"
        f"— signal: {opinion.verdict_signal} (read: {read_state})"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_consult_accounting.py tests/unit/test_tool_gating.py -v
.venv-test/bin/python -m pytest tests/characterization -v
```
Expected: PASS. The characterization run is the check that no `pi_lab` golden master
moved — this touches a hub-only path, so none should. **If one moves, stop; do not
regenerate.**

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/tools.py tests/unit/test_consult_accounting.py
git commit -m "fix(specialists): put the opinion before the label in the hub's context"
```

---

## Task 7: Pin the no-cross-anchoring invariant

No consult in 1,192 carries a sibling's verdict or a numeric score in its question or
context. That is the literature's strongest prohibition for a critic panel, and it
currently holds by accident.

**Files:**
- Test: `tests/unit/test_specialist_no_anchoring.py` (create)

**Interfaces:** none — test-only task.

- [ ] **Step 1: Write the test**

```python
# tests/unit/test_specialist_no_anchoring.py
"""A specialist must never see another specialist's verdict, or any score.

Anchoring on a prior score reaches Cohen's d = 0.71, blocks 48% of error
corrections, and is NOT removable by telling the model to ignore it
(arXiv:2608.25869). Measured across all 1,192 production consults on 2026-08-28:
zero contexts carried a sibling signal or a numeric score. This test is what
keeps that true — it held by accident until now.
"""
import re

import pytest

from src.agent.specialists import VERDICT_SIGNALS
from src.agent.tools import _execute_consult_specialist

_SCORE = re.compile(r"\b\d\.\d{1,2}\b")


@pytest.mark.asyncio
async def test_the_prompt_sent_to_a_specialist_carries_no_sibling_verdict(
    fake_anthropic,
):
    fake_anthropic.queue_text(
        '{"verdict_signal": "caution", "concerns": [], '
        '"questions_to_ask": [], "confidence": "low"}'
    )
    await _execute_consult_specialist(
        "legal",
        "How ownable is this given the chemistry specialist said blocking?",
        "PI: nothing is filed. Weighted score so far 2.85.",
        agent_id="blackbird",
    )

    sent = fake_anthropic.last_request
    body = sent["system"] + "".join(
        m["content"] for m in sent["messages"] if isinstance(m["content"], str)
    )

    assert "verdict_signal" not in body, (
        "the sidecar key must never reach a specialist's context"
    )
    for signal in VERDICT_SIGNALS:
        assert not re.search(rf"\b{signal}\b\s*(signal|verdict)", body, re.I), (
            f"a sibling {signal!r} verdict reached the specialist"
        )
    assert not _SCORE.search(body), "a numeric score reached the specialist"
```

Adapt `fake_anthropic` / `last_request` to the fixture and attribute names
`tests/fakes.py` already exposes for inspecting the last request.

**If this test fails on the hub's own question text** (the hub composed a question
naming a sibling's verdict, as the fixture above does deliberately), that is a real
finding, not a broken test: it means the hub can leak a verdict through its own
`question` argument. Record it and make the sanitisation the fix — do not weaken the
assertion.

- [ ] **Step 2: Run test**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialist_no_anchoring.py -v`
Expected: PASS if the path is clean; a FAILURE here is a discovered defect to report.

- [ ] **Step 3: Run the gate and commit**

```bash
./scripts/ci.sh
git add tests/unit/test_specialist_no_anchoring.py
git commit -m "test(specialists): pin the no-cross-anchoring invariant"
```

---

## Task 8: Migration — read_state, established, and the first rubric stamp on consults

`specialist_consults` has never carried a rubric stamp, which is exactly why pre- and
post-change consults cannot be compared. Assessments have had one since `0030`.

**Files:**
- Create: `alembic/versions/0038_specialist_consult_read_state_and_stamp.py`
- Modify: `src/models/specialist_consult.py` (after `truncated`)
- Modify: `src/agent/simulation.py` — `_record_specialist_consult` (`:4300` signature, `:4352` construction) and `_post_panel_note`'s sibling writer
- Test: `tests/integration/test_specialist_consult_model.py` (append)

**Interfaces:**
- Consumes: `read_state` (Task 3).
- Produces: columns `read_state`, `established`, `rubric_version`, `rubric_content_hash` on `SpecialistConsult`. Task 9 writes `established`; Task 11 reads `rubric_version` when rendering mixed vocabularies.

**Migration number:** CLAUDE.md reserves `0038` for the deferred `users.is_admin`
drop, which is **unwritten**. Confirm with `ls alembic/versions/ | sort | tail -3`
and `alembic heads` before naming the file; if something already claims `0038`, use
`0039` and update every reference in this task.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/integration/test_specialist_consult_model.py
"""The four columns added by 0038, and the two invariants that matter.

`read_state` NULL means "written before 0038", which is a third state and not
"unread" — the same reasoning `truncated`'s comment records for 0036.
"""
import pytest
from sqlalchemy import select

from src.models.specialist_consult import SpecialistConsult


@pytest.mark.asyncio
async def test_the_four_new_columns_round_trip(db_session):
    row = SpecialistConsult(
        simulation_run_id=None, agent_id="blackbird", domain="legal",
        question="q", verdict_signal="caution", confidence="low",
        raw_opinion="{}", truncated=False,
        read_state="defaulted",
        established=["the assignment chain is clean"],
        rubric_version="3.2.0", rubric_content_hash="42aec0479ac6",
    )
    db_session.add(row)
    await db_session.commit()

    got = (await db_session.execute(select(SpecialistConsult))).scalars().one()
    assert got.read_state == "defaulted"
    assert got.established == ["the assignment chain is clean"]
    assert got.rubric_version == "3.2.0"
    assert got.rubric_content_hash == "42aec0479ac6"


@pytest.mark.asyncio
async def test_all_four_are_nullable_so_old_rows_still_load(db_session):
    row = SpecialistConsult(
        simulation_run_id=None, agent_id="blackbird", domain="legal",
        question="q", verdict_signal="caution", confidence="low",
        raw_opinion="{}", truncated=False,
    )
    db_session.add(row)
    await db_session.commit()
    got = (await db_session.execute(select(SpecialistConsult))).scalars().one()
    assert got.read_state is None
    assert got.established is None


@pytest.mark.asyncio
async def test_established_none_lands_as_sql_null_not_the_json_null_scalar(
    db_session,
):
    """Same reasoning as `concerns`: two physical encodings of "absent" is a
    bug `WHERE established IS NULL` cannot see. See migration 0031."""
    row = SpecialistConsult(
        simulation_run_id=None, agent_id="blackbird", domain="legal",
        question="q", verdict_signal="caution", confidence="low",
        raw_opinion="{}", truncated=False, established=None,
    )
    db_session.add(row)
    await db_session.commit()
    found = (await db_session.execute(
        select(SpecialistConsult).where(SpecialistConsult.established.is_(None))
    )).scalars().all()
    assert len(found) == 1
```

Use the session fixture the file already uses.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_specialist_consult_model.py -k new_columns -v`
Expected: FAIL — `TypeError: 'read_state' is an invalid keyword argument`

- [ ] **Step 3: Write the migration and map the columns**

Create `alembic/versions/0038_specialist_consult_read_state_and_stamp.py`:

```python
"""specialist_consults: read_state, established, and the first rubric stamp.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-28 00:00:00.000000

Four additive nullable columns.

``read_state`` splits "we could not read this reply" out of ``verdict_signal``,
which until now carried both meanings: ``parse_opinion`` defaults an unreadable
reply to ``caution``, so a defaulted opinion was byte-indistinguishable from a
genuinely cautious one and only a WARNING line recorded the difference. NULL
means "written before this revision" — a third state, deliberately not
backfilled as ``parsed``, since guessing would manufacture exactly the
confidence the column exists to stop asserting.

``established`` is the specialist contract's first positive-evidence field.
Three of the nine ``clear`` opinions ever emitted filed a positive finding
inside the ``concerns`` array with a hedge appended, because ``concerns`` and
``questions_to_ask`` were the only content fields and both are negative-valence.

``rubric_version``/``rubric_content_hash`` are the stamp consults have never
had. ``opportunity_assessments`` has carried one since 0030; without it on this
table there is no way to tell which rubric — or, after the stage-bar change,
which bars — a stored consult was judged against. NULL on every pre-0038 row.

Deploy order: additive and nullable, so OLD code against the NEW schema is safe.
The reverse is not — the new code maps all four, so every
``select(SpecialistConsult)`` (the discussions panel cards at
src/services/thread_panel.py, both assessment detail pages at
src/services/assessment_detail.py, the admin router) and the engine's
``_record_specialist_consult`` INSERT raise ``UndefinedColumn`` against a
pre-0038 database. Build, migrate from a one-off container, then start — the
same ordering as 0028/0030/0036/0037 (see CLAUDE.md).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "specialist_consults",
        sa.Column("read_state", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "specialist_consults",
        sa.Column("established", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "specialist_consults",
        sa.Column("rubric_version", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "specialist_consults",
        sa.Column("rubric_content_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("specialist_consults", "rubric_content_hash")
    op.drop_column("specialist_consults", "rubric_version")
    op.drop_column("specialist_consults", "established")
    op.drop_column("specialist_consults", "read_state")
```

In `src/models/specialist_consult.py`, after the `truncated` column:

```python
    # Which of specialists.READ_STATES this consult's reply was in: parsed,
    # defaulted, or truncated. NULL means "written before 0038" — a third state,
    # not "parsed". Derived in code by `read_state_for`, never asked of the
    # model: it is a fact about the reply, not a judgement about the idea.
    read_state: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The specialist contract's positive-evidence field: what the record DOES
    # establish in this domain. `none_as_null=True` for the same reason
    # `concerns` has it — see migration 0031.
    established: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # Which rubric — and therefore which stage bars — this consult was judged
    # against. Assessments have carried this since 0030; consults never have,
    # which is why no pre-2026-08-28 consult can be compared with a later one.
    rubric_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rubric_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
```

In `simulation.py::_record_specialist_consult`, add the four keyword parameters
(defaulting `read_state=None`, `established=None`) and pass them into the
`SpecialistConsult(...)` construction at `:4352`, stamping the rubric from the
already-imported loader:

```python
                    read_state=read_state,
                    established=list(established) if established else None,
                    rubric_version=load_rubric().version,
                    rubric_content_hash=load_rubric().content_hash,
```

- [ ] **Step 4: Run tests and the migration round trip**

```bash
.venv-test/bin/python -m pytest tests/integration/test_specialist_consult_model.py -v
.venv-test/bin/python -m pytest tests/unit/test_json_none_as_null.py -v
./scripts/ci.sh   # includes the upgrade->downgrade->upgrade round trip
```
Expected: PASS, and `alembic heads` reports a single head.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0038_specialist_consult_read_state_and_stamp.py \
        src/models/specialist_consult.py src/agent/simulation.py \
        tests/integration/test_specialist_consult_model.py
git commit -m "feat(specialists): 0038 — read_state, established, and a rubric stamp on consults"
```

> **Deploy note for the operator, to be flagged when this lands:** migrate BEFORE the
> new code serves. `$DC build blackbird-app worker` → `$DC run --rm blackbird-app
> alembic upgrade head` → confirm `alembic current` equals `alembic heads` → `$DC up
> -d blackbird-app worker`, then `$DC --profile agent build agent` separately.

---

# PHASE B — the model contract changes

## Task 9: `established` in the persona contract, and the verdict last

**Files:**
- Modify: all 8 of `prompts/specialists/*.md` (the `## Answer format` block)
- Modify: `src/agent/specialists.py` — `parse_opinion` reads `established`; `SpecialistOpinion` gains the field
- Modify: `src/agent/tools.py` — forward `established` to `on_consult_record`
- Test: `tests/unit/test_specialists.py`, `tests/unit/test_specialist_persona_contract.py` (create)

**Interfaces:**
- Consumes: the `established` column (Task 8).
- Produces: `SpecialistOpinion.established: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_specialist_persona_contract.py
"""The eight personas share one answer contract. These pin the two properties
that make the contract work, across all eight files at once — a per-file edit
that misses one is the failure mode this catches.
"""
import re

import pytest

from src.agent.specialists import SPECIALIST_DOMAINS, persona_path


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_the_verdict_field_comes_after_the_evidence_fields(domain):
    """A model generates left to right, so a schema that names the verdict first
    commits to a label before writing any evidence. Evidence-before-rating is
    worth +6 to +11 accuracy points (arXiv:2305.17926)."""
    text = persona_path(domain).read_text(encoding="utf-8")
    assert text.index('"established"') < text.index('"verdict_signal"')
    assert text.index('"concerns"') < text.index('"verdict_signal"')
    assert text.index('"questions_to_ask"') < text.index('"verdict_signal"')


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_every_persona_asks_for_positive_evidence(domain):
    text = persona_path(domain).read_text(encoding="utf-8")
    assert '"established"' in text, (
        "without a positive field, specialists file positives inside `concerns`"
    )


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_no_persona_declares_a_label_too_long_for_the_column(domain):
    """`verdict_signal` is String(10). A label that does not fit is a silent
    truncation or a write failure at runtime."""
    text = persona_path(domain).read_text(encoding="utf-8")
    m = re.search(r'"verdict_signal":\s*"([^"]+)"', text)
    assert m
    for label in (part.strip() for part in m.group(1).split("|")):
        assert len(label) <= 10, f"{label!r} exceeds String(10)"
```

```python
# append to tests/unit/test_specialists.py
def test_established_is_parsed_when_present():
    op = parse_opinion(
        '{"established": ["assignment chain is clean"], "concerns": [],'
        ' "questions_to_ask": [], "verdict_signal": "clear", "confidence": "high"}',
        domain="legal",
    )
    assert op.established == ("assignment chain is clean",)


def test_established_defaults_to_empty_when_absent():
    """Every pre-change reply omits it; absence must not be an error."""
    op = parse_opinion(_raw(verdict_signal="caution"), domain="legal")
    assert op.established == ()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv-test/bin/python -m pytest tests/unit/test_specialist_persona_contract.py -v
.venv-test/bin/python -m pytest tests/unit/test_specialists.py -k established -v
```
Expected: FAIL on all — `"established"` absent from the personas, and
`SpecialistOpinion` has no such attribute.

- [ ] **Step 3: Edit the eight personas and the parser**

In **each** of `prompts/specialists/{budget,chemistry,clinical,commercial,legal,scientific,talent,technologic}.md`, replace the JSON block under `## Answer format` with:

```
{
  "established": ["what the record DOES support in your domain"],
  "concerns": ["one specific concern per entry"],
  "questions_to_ask": ["a question the hub should put to the PI, in the PI's language"],
  "verdict_signal": "blocking | caution | clear",
  "confidence": "high | moderate | low"
}
```

and insert this bullet immediately above the existing `- **blocking**` line:

```
- **established** — name what the record supports, not only what it lacks. A
  specialist that lists only concerns is reporting half of what it found.
```

Leave the three label definitions **unchanged** in this task — the rename is Task 11.

In `specialists.py`, add to `SpecialistOpinion`:

```python
    # What the record DOES establish in this domain. Positive-valence, and the
    # only such field in the contract: before it existed the schema could
    # express nothing but problems, so specialists filed positives inside
    # `concerns` with a hedge appended ("this is the strongest positive signal
    # in my domain and I have no counter-evidence against it, but...").
    established: tuple[str, ...] = ()
```

and in `parse_opinion`, populate it in the successful return alongside `concerns`:

```python
        established=_str_tuple(data.get("established")),
```

The early-default return keeps `established=()` by omission — an unreadable reply
established nothing.

In `tools.py`, add to the `on_consult_record(...)` call:

```python
                established=list(opinion.established),
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_specialist_persona_contract.py tests/unit/test_specialists.py -v
.venv-test/bin/python -m pytest tests/unit/test_rubric_prompt_sync.py -v
```
Expected: PASS

- [ ] **Step 5: Run the ladder, then the gate, then commit**

Run `scripts/panel_calibration_ladder.py` for real and diff against
`ladder-baseline-preB.json`. **Acceptance:** `WEAK`-tier `blocking`+`caution` share
≥ 85%, and R at one rung not below 0.594. A drop in either means the schema reorder
cost discrimination — stop and report rather than proceeding to Task 10.

```bash
./scripts/ci.sh
git add prompts/specialists/ src/agent/specialists.py src/agent/tools.py \
        tests/unit/test_specialist_persona_contract.py tests/unit/test_specialists.py
git commit -m "feat(specialists): add `established`, and put the verdict after the evidence"
```

---

## Task 10: Propagate the rubric's stage bars into the personas

The rubric already states what is adequate at incubation stage, globally and per
domain. `render_rubric_markdown()` puts it in front of the **hub**
(`agent.py:322`); `_execute_consult_specialist` reads the persona file **raw**
(`tools.py:593`). The specialists have never seen any of it.

**Files:**
- Modify: `prompts/rubric/blackbird-rubric.toml` (add `[stage_bar.*]`, bump `[meta].version` to `3.3.0`)
- Modify: `src/services/blackbird_rubric.py` (`StageBar` dataclass, parse + validate, `render_stage_bar_markdown`)
- Modify: `src/agent/tools.py` (render into the persona at `:593`)
- Modify: all 8 `prompts/specialists/*.md` (add the `{stage_bar}` placeholder)
- Modify: `src/services/directory.py:421` (key the "who to ask" hint off the bars)
- Test: `tests/unit/test_rubric_prompt_sync.py`, `tests/unit/test_stage_bars.py` (create)

**Interfaces:**
- Consumes: `Rubric` (fields listed at `blackbird_rubric.py:105-128`).
- Produces: `Rubric.stage_bars: dict[str, StageBar]`; `StageBar(domain: str, source: str, text: str)`; `render_stage_bar_markdown(domain: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_stage_bars.py
"""Stage bars are EXTRACTED from the rubric, not authored.

Each bar records the `source` clause it condenses so a reviewer can check the
condensation against the original, and so this test can assert the source
exists. Before this existed, every persona judged against a standard the
document explicitly disclaims — `legal` most sharply, which says three separate
times that unresolved FTO is the normal starting condition and not a
disqualifier, and which has never cleared once in 91 consults.
"""
import pytest

from src.agent.specialists import SPECIALIST_DOMAINS
from src.services.blackbird_rubric import (
    RubricError, load_rubric, render_stage_bar_markdown,
)


def test_every_specialist_domain_has_a_stage_bar():
    missing = set(SPECIALIST_DOMAINS) - set(load_rubric().stage_bars)
    assert not missing, f"no stage bar for {sorted(missing)}"


def test_no_stage_bar_names_a_domain_that_does_not_exist():
    extra = set(load_rubric().stage_bars) - set(SPECIALIST_DOMAINS)
    assert not extra, f"stage bar for unknown domain {sorted(extra)}"


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_every_bars_source_names_a_real_dimension_gate_or_red_flag(domain):
    """`source` is what makes the condensation auditable. A source naming
    nothing means the bar has drifted from the document it claims to quote."""
    r = load_rubric()
    valid = (
        {d.key for d in r.dimensions}
        | set(r.gating)
        | {"red_flags", "scoring_preamble"}
    )
    for named in load_rubric().stage_bars[domain].source.split(","):
        assert named.strip() in valid, f"{domain}: unknown source {named!r}"


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_the_rendered_bar_carries_the_global_incubation_sentence(domain):
    """The one-sentence global bar goes to all eight, unchanged: 'never the
    replicated data, filed IP, or identified syndicate a later-stage deal would
    show.'"""
    out = render_stage_bar_markdown(domain)
    assert "incubation grain" in out
    assert load_rubric().stage_bars[domain].text in out


def test_a_bar_with_an_unknown_source_is_rejected_at_import(tmp_path):
    doc = (
        Path("prompts/rubric/blackbird-rubric.toml").read_text(encoding="utf-8")
        + '\n[stage_bar.legal]\nsource = "no_such_dimension"\ntext = "x"\n'
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(doc, encoding="utf-8")
    with pytest.raises(RubricError, match="stage_bar"):
        parse_rubric(bad)
```

Add `from pathlib import Path` and `parse_rubric` to the imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_stage_bars.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_stage_bar_markdown'`

- [ ] **Step 3: Add the bars to the document**

Append to `prompts/rubric/blackbird-rubric.toml`, and bump `[meta].version` to
`"3.3.0"` with a `[meta].changelog` entry recording that this release adds stage bars
and changes no weight, threshold, dimension or gating key.

Each `text` is a condensation of the clause its `source` names. **Do not paraphrase
beyond the source** — the whole point is that no new policy is authored.

```toml
# ---------------------------------------------------------------------------
# Per-domain stage bars, rendered into prompts/specialists/*.md via the
# {stage_bar} placeholder. EXTRACTED, not authored: every `text` condenses the
# clause named in `source`, so a reviewer can check it against the original and
# tests/unit/test_stage_bars.py can assert the source exists.
#
# Why this table exists at all: render_rubric_markdown() puts the rubric in
# front of the HUB (src/agent/agent.py) and the specialists were never included
# (src/agent/tools.py reads the persona raw). For the panel's entire history it
# judged against standards this document explicitly disclaims — `legal` says
# three separate times that unresolved FTO is the normal starting condition, and
# `legal` has never cleared once in 91 consults.
# ---------------------------------------------------------------------------

[stage_bar.scientific]
source = "scientific_credibility, credible_science"
text = "Adequate here is a credible, testable mechanism whose key EXISTING results can be believed — controls, replication, interpretation. Judge the evidence they have, not evidence the stage hasn't produced: animal rescue is a 5, not the bar for a 4. IP is not required at this stage."

[stage_bar.chemistry]
source = "translational_path"
text = "Adequate here is a plausible modality and starting point with an articulable route toward a development candidate — tractability, not progress. Liabilities identified with a plan to test them early is adequate: ignorance of a risk scores low, but absence of data at this stage does not."

[stage_bar.clinical]
source = "differentiation_unmet_need"
text = "Adequate here is a real clinical decision point with a downstream intervention. Order-of-magnitude prevalence or TAM suffices — actionability over precision. Precise patient numbers are not the bar."

[stage_bar.commercial]
source = "differentiation_unmet_need, venture_potential"
text = "Adequate here is a differentiated mechanism aimed at an actionable need, and — if the science works — a company or license a VC or pharma would plausibly want. A named syndicate is not the bar at this stage."

[stage_bar.legal]
source = "venture_potential, translational_potential, red_flags"
text = "Adequate here is a clean PATH to ownable IP: a disclosure filed or filable, no known encumbrance or hostile co-ownership, and a plausible university license path. 'FTO secured' is NOT the bar — freedom to operate is diligence, not a gate, and unresolved FTO on unpublished academic science is the normal starting condition, not a disqualifier. Reserve blocking for IP genuinely unresolvable: key rights co-owned by an uncooperative third party with no plausible license path."

[stage_bar.technologic]
source = "venture_potential"
text = "Adequate here is a claim whose reach is stated honestly. A reusable platform generating a pipeline scores above one shot on goal, but a SINGLE ASSET IS THE NORMAL SHAPE of a de-risking grant — mark it down only where a clean result would still leave nothing worth building. Absence of independent validation at this stage is expected and scores nothing down on its own."

[stage_bar.talent]
source = "team_executability"
text = "Adequate here is PI credibility and lab capability to execute the de-risking plan in 12–24 months, with complementary expertise IDENTIFIED, not necessarily hired."

[stage_bar.budget]
source = "fundable_experiment"
text = "Adequate here is a scope where a $100K–$1M grant over 12–24 months could buy a DECISIVE de-risking result. Below the bar is no articulable experiment, scope far beyond an incubation grant, or key data unreplicable for under a $200K budget and a reasonable timeline."
```

- [ ] **Step 4: Parse, validate, and render**

In `blackbird_rubric.py`, add the dataclass and wire it into `parse_rubric`:

```python
@dataclass(frozen=True)
class StageBar:
    """One domain's "what is adequate at incubation stage", condensed from the
    clause named in ``source``. ``source`` is not decoration: it is what makes
    the condensation auditable and what
    tests/unit/test_stage_bars.py validates against the real keys."""

    domain: str
    source: str
    text: str
```

Add `stage_bars: dict[str, StageBar]` to `Rubric`, parse the `stage_bar` table in
`parse_rubric`, and validate each `source` against
`{d.key for d in dimensions} | set(gating) | {"red_flags", "scoring_preamble"}`,
raising `RubricError(f"rubric document: stage_bar.{domain} names unknown source ...")`
on a miss. Then:

```python
def render_stage_bar_markdown(domain: str) -> str:
    """The stage-bar section a specialist persona carries.

    Fills the ``{stage_bar}`` placeholder in prompts/specialists/<domain>.md,
    exactly as ``render_rubric_markdown`` fills ``{rubric}`` for the hub — so
    the bar a specialist judges against and the anchors the hub scores against
    cannot drift apart. Raises for an unknown domain rather than rendering
    nothing: a silently bar-less persona is the defect this function exists to
    end.
    """
    bar = _RUBRIC.stage_bars.get(domain)
    if bar is None:
        raise RubricError(f"rubric document: no stage_bar for domain {domain!r}")
    return "\n".join([
        "## The bar at this stage",
        "",
        _RUBRIC.scoring_preamble.strip(),
        "",
        bar.text,
        "",
        f"(Source: {bar.source}, rubric {_RUBRIC.version} / "
        f"{_RUBRIC.content_hash[:12]}.)",
    ])
```

In each persona file, add a `{stage_bar}` line immediately after the opening
paragraph and before `## What you own`. In `tools.py`, replace `:593`:

```python
    persona = path.read_text(encoding="utf-8")
    # The specialists were never inside the mechanism that keeps the hub's
    # prompt and the document in step (render_rubric_markdown -> {rubric},
    # src/agent/agent.py). str.replace, NOT str.format: persona files contain
    # bare curly braces, the same reason _render_identity uses replace.
    if _STAGE_BAR_PLACEHOLDER in persona:
        persona = persona.replace(
            _STAGE_BAR_PLACEHOLDER, render_stage_bar_markdown(domain)
        )
```

with `_STAGE_BAR_PLACEHOLDER = "{stage_bar}"` at module level.

In `directory.py:421`, extend `specialist_for` so a domain with no
`maps_to_dimensions` is still reachable via its bar's `source`:

```python
    # `maps_to_dimensions` is empty for clinical, legal and technologic since
    # v3 folded market_unmet_need, ip_fto and platform into the consolidated
    # dimensions. Their stage bar's `source` still names the absorbing
    # dimension, so the "who to ask" hint stays complete.
    for domain, bar in load_rubric().stage_bars.items():
        for named in (s.strip() for s in bar.source.split(",")):
            specialist_for.setdefault(named, domain)
```

- [ ] **Step 5: Extend the sync tests and run everything**

Add to `tests/unit/test_rubric_prompt_sync.py`:

```python
@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_every_persona_carries_the_stage_bar_placeholder(domain):
    """A persona without the placeholder silently keeps its bar-less behaviour —
    which is the defect this whole change exists to fix, reintroduced by a
    forgotten file."""
    assert "{stage_bar}" in persona_path(domain).read_text(encoding="utf-8")
```

```bash
.venv-test/bin/python -m pytest tests/unit/test_stage_bars.py tests/unit/test_rubric_prompt_sync.py -v
.venv-test/bin/python -m pytest tests/unit/test_blackbird_rubric.py tests/unit/test_rubric_document.py tests/unit/test_panel_state.py -v
./scripts/ci.sh
```
Expected: PASS. `[meta].version` moved to `3.3.0`, so any assertion on the version
string must be updated deliberately — check `tests/unit/test_rubric_document.py` and
`tests/unit/test_blackbird_rubric.py` first, and treat a version assertion the same
way the retired clear-rate floor's pinned constant was treated: the change should be
an explicit diff with its reasoning in the docstring, not a silent edit.

- [ ] **Step 6: Regenerate the review doc, run the ladder, commit**

The standing operator directive: after any rubric change, regenerate the
human-reviewable export and confirm the hash matches.

```bash
.venv-test/bin/python scripts/render_rubric_review_doc.py
ls docs/rubric-review/   # expect blackbird-rubric-v3.3.0-<hash>-review.md
```

Then run the ladder. **Acceptance for this task:** `legal`'s verdict must change
across quality tiers (it was 0 of 2), and the top label must be reached by at least
one domain outside `budget`/`talent`/`scientific`. `WEAK`-tier
`blocking`+`caution` ≥ 85%. If `legal` still does not move, **report it** — do not
reword the bar until the number moves.

```bash
git add prompts/rubric/blackbird-rubric.toml prompts/specialists/ \
        src/services/blackbird_rubric.py src/services/directory.py \
        src/agent/tools.py docs/rubric-review/ \
        tests/unit/test_stage_bars.py tests/unit/test_rubric_prompt_sync.py
git commit -m "feat(rubric): v3.3.0 — propagate per-domain stage bars into the specialist personas"
```

---

## Task 11: The vocabulary rename

Ships **only** on a passing Task 10 ladder. `adequate` without a bar behind it is a
renamed `clear`.

**Files:**
- Modify: `src/agent/specialists.py` — `VERDICT_SIGNALS` (`:39`), `_DEFAULT_SIGNAL` (`:43`), `_PANEL_NOTE_SIGNAL_EMOJI` (`:150-154`), `format_panel_note` (`:172-195`)
- Modify: all 8 `prompts/specialists/*.md` (the three label definitions)
- Modify: `src/services/thread_panel.py`, `src/services/assessment_detail.py` (render five values, add concern count)
- Test: `tests/unit/test_specialists.py`, `tests/integration/test_discussions_panel_cards.py`

**Interfaces:**
- Consumes: the stage bars (Task 10).
- Produces: `VERDICT_SIGNALS = {"blocking", "gap", "adequate"}`; `HISTORICAL_VERDICT_SIGNALS = {"caution", "clear"}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_specialists.py
def test_the_live_vocabulary_is_pinned():
    """Pinned literally so a future change is a diff, not a drift — the same
    discipline the retired clear-rate floor had."""
    assert VERDICT_SIGNALS == frozenset({"blocking", "gap", "adequate"})


def test_the_conservative_default_is_gap_never_adequate():
    """An unread specialist must not read as approval. This is the safety
    property `_DEFAULT_SIGNAL = "caution"` was protecting."""
    op = parse_opinion("prose, not an object", domain="legal")
    assert op.verdict_signal == "gap"


def test_historical_values_are_still_renderable():
    """1,192 stored rows carry caution/clear. The read path must render them;
    only `blocking` survives the boundary with its meaning intact."""
    assert HISTORICAL_VERDICT_SIGNALS == frozenset({"caution", "clear"})
    for old in HISTORICAL_VERDICT_SIGNALS:
        note = format_panel_note(domain="legal", verdict_signal=old, question="q")
        assert old in note


def test_adequate_does_not_render_as_a_bare_approval_tick():
    """All nine `clear` opinions ever emitted carried 4-9 concerns; one listed
    "Succession risk is high as described" under a ✅. The label means "meets the
    bar for this stage", not "no concerns"."""
    note = format_panel_note(
        domain="budget", verdict_signal="adequate", question="does it fit the band?"
    )
    assert "adequate for stage" in note
    assert "✅" not in note


def test_the_panel_note_still_cannot_carry_opinion_content():
    """The narrow signature IS the enforcement (spec D7). Concern counts go to
    staff surfaces only."""
    import inspect
    params = set(inspect.signature(format_panel_note).parameters)
    assert params == {"domain", "verdict_signal", "question"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -k "vocabulary or adequate or historical" -v`
Expected: FAIL — `VERDICT_SIGNALS` still holds the old three.

- [ ] **Step 3: Rename**

In `specialists.py`:

```python
#: The live vocabulary. `blocking` is unchanged across the 2026-08-28 rename —
#: it carried all of the panel's measured information (chemistry is the most
#: informative domain at 0.914 bits and discriminates entirely through it), so
#: its 161 stored rows stay interpretable. `gap` and `adequate` replace
#: `caution` and `clear`, which are retained in HISTORICAL_VERDICT_SIGNALS
#: because 1,192 rows carry them and none was rewritten.
VERDICT_SIGNALS: frozenset[str] = frozenset({"blocking", "gap", "adequate"})

#: Pre-2026-08-28 values. Renderable, never emitted. `caution` was "a real
#: weakness that changes how much weight the result carries" — no materiality
#: threshold, so it absorbed 85.7% of all consults; `clear` was "nothing in your
#: domain stands in the way", a universal negative over a whole domain that six
#: of eight domains never once reached.
HISTORICAL_VERDICT_SIGNALS: frozenset[str] = frozenset({"caution", "clear"})

_DEFAULT_SIGNAL = "gap"
```

Emoji map — `adequate` deliberately does not get ✅:

```python
_PANEL_NOTE_SIGNAL_EMOJI: dict[str, str] = {
    "blocking": "⛔",
    "gap": "⚠️",
    # ☑️ not ✅: every `clear` opinion ever emitted carried 4-9 concerns, and
    # a tick reads as "no concerns" to the humans watching the thread. The
    # label means "meets the bar for THIS STAGE", which is what the text says.
    "adequate": "☑️",
    # Historical, still rendered for stored rows.
    "caution": "⚠️",
    "clear": "☑️",
}

_PANEL_NOTE_SIGNAL_LABEL: dict[str, str] = {"adequate": "adequate for stage"}
```

and in `format_panel_note`, render through the label map:

```python
    emoji = _PANEL_NOTE_SIGNAL_EMOJI.get(verdict_signal, "")
    wording = _PANEL_NOTE_SIGNAL_LABEL.get(verdict_signal, verdict_signal)
    signal = f"{emoji} {wording}".strip()
```

In **each** persona, replace the three definitions:

```
- **blocking** — a defect that disqualifies this opportunity in your domain as it
  stands.
- **gap** — the record falls short of the bar for this stage, AND you can name the
  specific thing that must be produced to reach it. A gap you cannot name is not a
  gap.
- **adequate** — the record meets the bar for this stage in your domain. This does
  NOT mean "no concerns": list them, and say the record is adequate anyway. Ground it
  in `established`.
```

and update the JSON block's enum to `"blocking | gap | adequate"`.

On the staff surfaces (`thread_panel.py:179`, `assessment_detail.py:245`/`:738`),
add the concern count and `read_state` to each card's dict, and render historical
values unchanged.

- [ ] **Step 4: Run everything**

```bash
.venv-test/bin/python -m pytest tests/unit/test_specialists.py tests/unit/test_specialist_persona_contract.py -v
.venv-test/bin/python -m pytest tests/integration/test_discussions_panel_cards.py tests/integration/test_assessment_detail_page.py -v
.venv-test/bin/python -m pytest tests/characterization -v
./scripts/ci.sh
```
Expected: PASS, `.ambr` unchanged.

- [ ] **Step 5: Final ladder run and commit**

**Acceptance:** `WEAK`-tier `blocking`+`gap` ≥ 85%; `legal` moves across tiers; R at
one rung ≥ 0.594; `adequate` reached by a domain outside
`budget`/`talent`/`scientific`. Save the run beside the baseline.

```bash
git add src/agent/specialists.py prompts/specialists/ \
        src/services/thread_panel.py src/services/assessment_detail.py \
        tests/ docs/audits/2026-08-27-consult-persona-calibration/
git commit -m "feat(specialists): rename the verdict vocabulary to blocking/gap/adequate"
```

---

## Self-review

**Spec coverage.** §3.1 labels → Task 11. §3.2 schema → Task 9. §3.3 read-state →
Tasks 3, 4, 8. §4 bars → Task 10. §5 consumers: hub → Task 6, Slack note → Tasks 4
and 11, staff surfaces → Task 11, alarm → Task 5. §6 migration → Task 8. §7.1 harness
→ Task 2. §7.2 criteria → gate steps of Tasks 9, 10, 11. §7.4 tests: vocabulary →
11, read-state → 3, note cancellation → 4, signature not widened → 11, no-anchoring
→ 7, rubric sync → 10, alarm wording → 5. §4.5 side effect → Task 10.

**One spec item deliberately without a task:** §9's ~100 human-labelled anchors is
operator time, not code, and is listed out of scope in the spec.

**Type consistency.** `read_state_for(*, truncated, opinion) -> str` (Task 3) is
called with those keywords in Tasks 3–4 and stored as `read_state` in Task 8.
`construct_sensitivity`/`invariance` return `(int, int)` in Task 1 and are formatted
as ratios in Task 2. `StageBar(domain, source, text)` (Task 10) is read as
`.source`/`.text` in Task 10's render and `directory.py` patch.
`SpecialistOpinion.established` is a `tuple[str, ...]` (Task 9) and is converted with
`list(...)` at the JSONB boundary, matching how `concerns` is already handled.

**Placeholder scan.** No "TBD"/"TODO"/"add error handling"/"similar to Task N". Two
places name an adaptation rather than exact code — the fixture names in Tasks 4, 6
and 7 — because those fixtures already exist in the target files under names this
plan should not guess at; each says which file to take them from.
