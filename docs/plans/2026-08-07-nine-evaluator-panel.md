# Nine-Evaluator Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Blackbird scouting hub eight consultable specialist perspectives and four scored scientific dimensions, so that the two things BBL actually rejects on — science and chemistry — can both be asked about during an interview and recorded in the verdict.

**Architecture:** One new dependency-free module, `src/agent/specialists.py`, owns the eight personas and the opinion contract. `consult_specialist` joins `TOOL_DEFINITIONS` and `scout_hub`'s `role.toml` allow-list, reachable only from the phase-4 interview (the only turn with a tool loop). Consults accumulate on a per-thread map on `SimulationEngine`, and `_persist_assessment` refuses an `advance`/`conditional` verdict whose required domains were never consulted. Separately, `RUBRIC_WEIGHTS` gains four scientific dimensions at 40% against 60% commercial.

**Tech Stack:** Python 3.11, Anthropic tool-use API, pytest, SQLAlchemy async, Docker Compose.

**Spec:** `docs/specs/2026-08-07-nine-evaluator-panel-design.md`

## Global Constraints

- **Test command is on the HOST, never through the mount and never in a container.** This checkout is an sshfs mount; pytest through it is ~1000x slower and times out. Use:
  `ssh -o BatchMode=yes ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest <args>'`
  Edit files and run `git` locally. **Never run any `docker` command on that host** — it serves two production stacks.
- **Full gate before any commit is considered done:** `./scripts/ci.sh` on the host. Baseline to beat: **1820 passed, 0 failed, 122 skipped, coverage 67.79%**, `src/` ruff **259** against a ceiling of **260 — one finding of headroom**.
- **Ruff:** `select = ["E","F","I","UP","B"]`, `ignore = ["E501"]`, target py311. `I` enforces import order. Zero findings required on `tests/`.
- **`pyproject.toml` sets `asyncio_mode = "auto"`** — async tests need no `@pytest.mark.asyncio`.
- **`src/agent/specialists.py` must stay dependency-free of the engine** — no `src.models`, no DB, no `SimulationEngine` import. It may import from `src.services.llm`. This is what keeps it unit-testable, and it mirrors `src/agent/post_types.py` and `src/agent/roles.py`.
- **`consult_specialist` is reachable ONLY from phase 4.** `_reply_to_thread` (`src/agent/simulation.py:1373`) is the only call site passing tools. Do not add a tool loop to phase 5.
- **No migration.** `scores` is JSONB; `_specialist_consults` is in-memory. Do not add one.
- **⚠️ `profiles/` is NOT in version control.** `.gitignore:36` (`profiles/**/*.md`) has excluded it since `18dfc17`; `git ls-files profiles/` returns zero. Edits to `profiles/private/blackbird.md` cannot be committed. The files are `root:root` on the host, so they need `sudo cp` over ssh, preserving ownership and mode. `profiles/` IS bind-mounted into the running agent container and re-read per call, so those edits go live without a rebuild. Say so in the report; never `git add -f` them.
- **ADD commits. Never `git commit --amend`, never rebase.** Other agents commit to this branch; an amend has already hit the wrong commit once on this branch.
- **Commit style:** end every commit message with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `src/agent/specialists.py` *(new)* | `SPECIALIST_DOMAINS`, `SpecialistOpinion`, `parse_opinion`, `required_domains_for`, `render_consult_prompt`. Pure functions over plain data. |
| `prompts/specialists/*.md` *(new, 8 files)* | One persona per domain. Loaded by path, like every other prompt. |
| `src/agent/tools.py` *(modify)* | `consult_specialist` in `TOOL_DEFINITIONS`; a dispatch branch; `_execute_consult_specialist`. |
| `prompts/roles/scout_hub/role.toml` *(modify)* | Add `consult_specialist` to `tools`. |
| `src/agent/simulation.py` *(modify)* | `_specialist_consults` map; record on consult; enforce the floor in `_persist_assessment`. |
| `src/services/blackbird_rubric.py` *(modify)* | The 13-dimension `RUBRIC_WEIGHTS`. |
| `profiles/private/blackbird.md` *(modify, untracked)* | Dimension table §3 and `scores` skeleton §6. |
| `prompts/roles/scout_hub/phase5-new-post.md` *(modify)* | The `scores` contract; the floor's consequence. |
| `prompts/roles/scout_hub/phase4-thread-reply.md` *(modify)* | When to consult, and that skipping costs the verdict. |
| `tests/unit/test_specialists.py` *(new)* | Domains, opinion parsing, requirement derivation. |
| `tests/unit/test_specialist_floor.py` *(new)* | The floor, table-driven. |
| `tests/unit/test_tool_gating.py` *(modify)* | `consult_specialist` gating. |
| `tests/unit/test_blackbird_rubric.py` *(modify)* | The weights pin. |

**Task order and dependencies:** 1 → 2 → 3 → 4 → 5, then 6 (independent of 2-5, may run in parallel with them if file sets are respected). Task 5 depends on Tasks 1 and 4. Task 6 touches only the rubric/weights files and no file any other task touches.

---

### Task 1: The specialists module

**Files:**
- Create: `src/agent/specialists.py`
- Test: `tests/unit/test_specialists.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SPECIALIST_DOMAINS: dict[str, SpecialistSpec]` — the eight, keyed by domain
  - `@dataclass(frozen=True) SpecialistSpec(domain: str, title: str, owns: str, consult_when: str, maps_to_dimension: str | None)`
  - `@dataclass(frozen=True) SpecialistOpinion(domain: str, verdict_signal: str, concerns: tuple[str, ...], questions_to_ask: tuple[str, ...], confidence: str, raw: str)`
  - `VERDICT_SIGNALS: frozenset[str]` — `{"blocking", "caution", "clear"}`
  - `parse_opinion(raw: str, *, domain: str) -> SpecialistOpinion`
  - `required_domains_for(verdict: dict) -> frozenset[str]`
  - `persona_path(domain: str) -> Path`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_specialists.py`:

```python
"""The eight specialist personas, the opinion contract, and the rule that
derives which domains a given verdict was obliged to consult.

Pure functions over plain data — no DB, no engine, no LLM. See
docs/specs/2026-08-07-nine-evaluator-panel-design.md §2, §4.
"""
import json

from src.agent.specialists import (
    SPECIALIST_DOMAINS,
    VERDICT_SIGNALS,
    parse_opinion,
    persona_path,
    required_domains_for,
)


def test_the_eight_domains_are_exactly_the_spec_table():
    assert set(SPECIALIST_DOMAINS) == {
        "scientific", "chemistry", "clinical", "commercial",
        "legal", "technologic", "talent", "budget",
    }


def test_blackbird_is_not_a_specialist():
    """The document lists Blackbird as the ninth evaluator, but it is the hub
    doing the integrating — not something the hub can consult about itself."""
    assert "blackbird" not in SPECIALIST_DOMAINS


def test_every_domain_declares_when_to_consult_it():
    for domain, spec in SPECIALIST_DOMAINS.items():
        assert spec.consult_when.strip(), domain
        assert spec.owns.strip(), domain
        assert spec.title.strip(), domain


def test_the_two_missing_personas_map_to_new_dimensions():
    """Scientific and Chemistry are the personas with no representation in the
    old rubric, and the reason the panel exists. Each must land somewhere."""
    assert SPECIALIST_DOMAINS["scientific"].maps_to_dimension == "experimental_rigor"
    assert SPECIALIST_DOMAINS["chemistry"].maps_to_dimension == "chemistry_dc_path"


def test_persona_path_is_under_prompts_specialists():
    p = persona_path("chemistry")
    assert p.as_posix().endswith("prompts/specialists/chemistry.md")


# --- parse_opinion -----------------------------------------------------------

def _raw(**over):
    body = {
        "verdict_signal": "caution",
        "concerns": ["in-family off-target risk at SK2"],
        "questions_to_ask": ["What selectivity margin over SK2 have you measured?"],
        "confidence": "moderate",
    }
    body.update(over)
    return json.dumps(body)


def test_parse_reads_the_contract():
    op = parse_opinion(_raw(), domain="chemistry")
    assert op.domain == "chemistry"
    assert op.verdict_signal == "caution"
    assert op.concerns == ("in-family off-target risk at SK2",)
    assert op.questions_to_ask == (
        "What selectivity margin over SK2 have you measured?",
    )
    assert op.confidence == "moderate"


def test_parse_keeps_the_raw_text_verbatim():
    """The hub sees the raw string; the parse is for us, not for it."""
    raw = _raw()
    assert parse_opinion(raw, domain="chemistry").raw == raw


def test_parse_of_prose_is_an_opinion_not_a_failure():
    """A specialist that answers in prose has still answered. Only a FAILED
    call must not satisfy the floor — see the engine, not here."""
    op = parse_opinion("The chemistry here is not close to a DC.", domain="chemistry")
    assert op.verdict_signal == "caution"
    assert op.concerns == ()
    assert op.raw == "The chemistry here is not close to a DC."


def test_parse_of_an_unknown_signal_degrades_to_caution():
    op = parse_opinion(_raw(verdict_signal="catastrophic"), domain="chemistry")
    assert op.verdict_signal == "caution"


def test_parse_of_a_fenced_block_still_works():
    """Models fence JSON by reflex. Do not let that be a parse failure."""
    op = parse_opinion("```json\\n" + _raw() + "\\n```", domain="chemistry")
    assert op.verdict_signal == "caution"


def test_every_signal_in_the_enum_round_trips():
    for sig in VERDICT_SIGNALS:
        assert parse_opinion(_raw(verdict_signal=sig), domain="legal").verdict_signal == sig


def test_non_list_concerns_degrade_to_empty():
    op = parse_opinion(_raw(concerns="a string, not a list"), domain="legal")
    assert op.concerns == ()


# --- required_domains_for ----------------------------------------------------

def test_scientific_and_talent_are_always_required():
    assert {"scientific", "talent"} <= required_domains_for({})


def test_chemical_matter_requires_chemistry():
    v = {"company_or_project": "A small molecule SCAP inhibitor"}
    assert "chemistry" in required_domains_for(v)


def test_a_modality_requires_chemistry():
    v = {"rationale": "An antisense oligonucleotide for ALS."}
    assert "chemistry" in required_domains_for(v)


def test_a_disease_claim_requires_clinical():
    v = {"rationale": "For the treatment of schizophrenia in adults."}
    assert "clinical" in required_domains_for(v)


def test_met_fto_requires_legal():
    """Claiming FTO is achievable is a legal assertion; it must be sourced."""
    v = {"gating": {"fto_achievable": "met"}}
    assert "legal" in required_domains_for(v)


def test_unconfirmed_fto_does_not_require_legal():
    v = {"gating": {"fto_achievable": "unconfirmed"}}
    assert "legal" not in required_domains_for(v)


def test_a_platform_claim_requires_technologic():
    v = {"scores": {"platform": 5}, "rationale": "A reusable editing platform."}
    assert "technologic" in required_domains_for(v)


def test_a_bare_verdict_requires_only_the_always_pair():
    assert required_domains_for({}) == frozenset({"scientific", "talent"})


def test_requirement_derivation_never_raises_on_junk():
    """This runs inside _persist_assessment's try block; an exception here
    would be logged as 'Failed to persist assessment' after the row committed."""
    for junk in (None, [], "string", {"gating": "not a dict"}, {"scores": 7}):
        assert isinstance(required_domains_for(junk), frozenset)
```

- [ ] **Step 2: Run the test to verify it fails**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'src.agent.specialists'`

- [ ] **Step 3: Write the implementation**

Create `src/agent/specialists.py`:

```python
"""The eight consultable specialist personas, and the rules around them.

The stakeholder document (BBL Evaluator Notes) names nine "Potential Agentic
Evaluators". Eight are consultable domains; the ninth, Blackbird, is the hub
doing the integrating and is therefore not in this table.

Dependency-free on the engine (no src.models, no DB, no SimulationEngine), like
src/agent/post_types.py and src/agent/roles.py, so the contract and the
requirement rule are unit-testable without a database or a running loop.

Why these eight and not some other cut: six of them already existed as weighted
rubric dimensions. The two that did not — scientific and chemistry — are the two
that decide real cases. Counted over the document's own 15 rejections: mechanism
validation 5, toxicity/selectivity 4, chemistry-to-DC-path 4. FTO appears once
and already carried a gate, a 10% dimension, a red flag and a dedicated tool.
See docs/specs/2026-08-07-nine-evaluator-panel-design.md §1.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path("prompts")
SPECIALISTS_DIR = PROMPTS_DIR / "specialists"

VERDICT_SIGNALS: frozenset[str] = frozenset({"blocking", "caution", "clear"})

# Unknown or missing signals degrade here rather than to "clear": a specialist
# whose answer we could not read has NOT cleared anything.
_DEFAULT_SIGNAL = "caution"
_CONFIDENCES: frozenset[str] = frozenset({"high", "moderate", "low"})


@dataclass(frozen=True)
class SpecialistSpec:
    """One consultable perspective. ``maps_to_dimension`` names the rubric
    dimension this specialist's concerns belong in, so a blocking signal has
    somewhere to land — None where the specialist informs judgement without
    owning a single dimension."""

    domain: str
    title: str
    owns: str
    consult_when: str
    maps_to_dimension: str | None = None


SPECIALIST_DOMAINS: dict[str, SpecialistSpec] = {
    s.domain: s
    for s in (
        SpecialistSpec(
            "scientific", "Scientific Specialist",
            "experimental rigor, controls, statistical power, interpretability, "
            "mouse-to-human translatability",
            "any experimental claim, any animal-model result, any 'we showed that'",
            maps_to_dimension="experimental_rigor",
        ),
        SpecialistSpec(
            "chemistry", "Chemistry Specialist",
            "path to a development candidate, medicinal-chemistry tractability, "
            "tolerability, in-family off-target liability",
            "chemical matter, a compound series, or a choice of modality",
            maps_to_dimension="chemistry_dc_path",
        ),
        SpecialistSpec(
            "clinical", "Clinical Specialist",
            "unmet need against the current standard of care, indication choice, "
            "patient numbers, the clinical development path",
            "any disease or indication claim",
            maps_to_dimension="market_unmet_need",
        ),
        SpecialistSpec(
            "commercial", "Commercial Specialist",
            "competitive landscape, named competing programs, deal comparables, "
            "investor sentiment",
            "any differentiation or first/best-in-class claim",
            maps_to_dimension="differentiation",
        ),
        SpecialistSpec(
            "legal", "Legal Specialist",
            "freedom to operate, licensing, research-tool and animal-model "
            "encumbrance, co-ownership",
            "any IP claim, or reliance on third-party materials",
            maps_to_dimension="ip_fto",
        ),
        SpecialistSpec(
            "technologic", "Technologic Specialist",
            "platform feasibility, and whether the proposed work would actually "
            "test that feasibility",
            "any platform or novel-technology claim",
            maps_to_dimension="platform",
        ),
        SpecialistSpec(
            "talent", "Talent Specialist",
            "probability the team completes the work, conflicts of interest, "
            "over-commitment across projects",
            "always, before concluding an interview",
            maps_to_dimension="team",
        ),
        SpecialistSpec(
            "budget", "Budget Specialist",
            "scope against Blackbird's grant bands and 12-24 month durations",
            "any workplan, cost, or timeline claim",
            maps_to_dimension="workplan_capital_efficiency",
        ),
    )
}


@dataclass(frozen=True)
class SpecialistOpinion:
    domain: str
    verdict_signal: str
    concerns: tuple[str, ...]
    questions_to_ask: tuple[str, ...]
    confidence: str
    raw: str


def persona_path(domain: str) -> Path:
    """Where a domain's persona file lives. Not validated here — the caller
    checks existence, because a missing file must not satisfy the floor."""
    return SPECIALISTS_DIR / f"{domain}.md"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_fence(raw: str) -> str:
    m = _FENCE_RE.match(raw)
    return m.group(1) if m else raw


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v.strip())


def parse_opinion(raw: str, *, domain: str) -> SpecialistOpinion:
    """Parse a specialist's reply. Never raises.

    Prose is a valid opinion — a specialist that answers in sentences has still
    answered, and the hub reads ``raw`` regardless. Only a call that FAILED must
    not satisfy the floor, and that is decided by the caller, not here.

    Models fence JSON by reflex, so a fenced block is unwrapped rather than
    treated as a parse failure.
    """
    text = _strip_fence(raw or "")
    data: object = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None

    if not isinstance(data, dict):
        return SpecialistOpinion(
            domain=domain, verdict_signal=_DEFAULT_SIGNAL, concerns=(),
            questions_to_ask=(), confidence="low", raw=raw,
        )

    signal = data.get("verdict_signal")
    if signal not in VERDICT_SIGNALS:
        signal = _DEFAULT_SIGNAL
    confidence = data.get("confidence")
    if confidence not in _CONFIDENCES:
        confidence = "low"

    return SpecialistOpinion(
        domain=domain,
        verdict_signal=signal,
        concerns=_str_tuple(data.get("concerns")),
        questions_to_ask=_str_tuple(data.get("questions_to_ask")),
        confidence=confidence,
        raw=raw,
    )


# Substrings that make a domain required. Deliberately broad: a false positive
# costs one consult, a false negative costs the whole point of the floor.
_CHEMISTRY_CUES = (
    "small molecule", "compound", "inhibitor", "activator", "antagonist",
    "agonist", "chemical matter", "medicinal chem", "antibody", "adc",
    "oligonucleotide", "aso", "sirna", "peptide", "modality", "scaffold",
    "lead series", "hit", "pharmacophore",
)
_CLINICAL_CUES = (
    "disease", "patient", "indication", "clinical", "therapeutic", "treatment",
    "cancer", "tumor", "tumour", "syndrome", "disorder", "epilepsy",
    "schizophrenia", "als", "neurodegener", "diagnosis",
)
_PLATFORM_CUES = ("platform", "pipeline", "multiple shots", "reusable")

_ALWAYS: frozenset[str] = frozenset({"scientific", "talent"})


def _haystack(verdict: dict) -> str:
    """Every free-text field of a verdict, lowercased, for cue matching."""
    parts: list[str] = []
    for key in ("company_or_project", "rationale", "funnel_stage", "recommendation"):
        value = verdict.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("red_flags", "suggested_derisking_milestones"):
        value = verdict.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
    return " ".join(parts).lower()


def required_domains_for(verdict: object) -> frozenset[str]:
    """Which specialists an ``advance``/``conditional`` verdict was obliged to
    consult, derived from the verdict's OWN content.

    Derived rather than self-reported on purpose: a hub that skipped Chemistry
    must not also get to declare Chemistry unnecessary.

    Never raises — this runs inside ``_persist_assessment``'s try block, where an
    exception would be caught and logged as "Failed to persist assessment"
    *after* the row had already been committed.
    """
    if not isinstance(verdict, dict):
        return _ALWAYS

    required = set(_ALWAYS)
    text = _haystack(verdict)

    if any(cue in text for cue in _CHEMISTRY_CUES):
        required.add("chemistry")
    if any(cue in text for cue in _CLINICAL_CUES):
        required.add("clinical")

    gating = verdict.get("gating")
    if isinstance(gating, dict) and gating.get("fto_achievable") == "met":
        required.add("legal")

    scores = verdict.get("scores")
    platform_scored = isinstance(scores, dict) and isinstance(
        scores.get("platform"), (int, float)
    ) and scores.get("platform", 0) >= 4
    if platform_scored or any(cue in text for cue in _PLATFORM_CUES):
        required.add("technologic")

    return frozenset(required)
```

- [ ] **Step 4: Run the test to verify it passes**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -q`
Expected: PASS, all tests.

- [ ] **Step 5: Lint**

Run on the host: `.venv-test/bin/python -m ruff check src/agent/specialists.py tests/unit/test_specialists.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/agent/specialists.py tests/unit/test_specialists.py
git commit -m "feat(specialists): the eight consultable personas and the opinion contract

Requirements are derived from the verdict's own content, never self-reported:
a hub that skipped Chemistry must not also get to declare Chemistry
unnecessary. Cues are deliberately broad — a false positive costs one consult,
a false negative costs the whole point of the floor.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The eight persona files

**Files:**
- Create: `prompts/specialists/scientific.md`, `chemistry.md`, `clinical.md`, `commercial.md`, `legal.md`, `technologic.md`, `talent.md`, `budget.md`
- Test: `tests/unit/test_specialists.py` (append)

**Interfaces:**
- Consumes: `persona_path` and `SPECIALIST_DOMAINS` (Task 1).
- Produces: eight files on disk. No Python.

**These are tracked by git** — unlike `profiles/`, `prompts/` is in version control. Commit them.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_specialists.py`:

```python
# --- the persona files -------------------------------------------------------

def test_every_domain_has_a_persona_file_on_disk():
    for domain in SPECIALIST_DOMAINS:
        p = persona_path(domain)
        assert p.is_file(), f"missing persona file for {domain}: {p}"
        assert p.read_text(encoding="utf-8").strip(), f"empty persona file: {p}"


def test_no_orphan_persona_files():
    """A file with no domain is a file nothing can ever load."""
    from src.agent.specialists import SPECIALISTS_DIR

    on_disk = {p.stem for p in SPECIALISTS_DIR.glob("*.md")}
    assert on_disk == set(SPECIALIST_DOMAINS)


def test_every_persona_states_the_opinion_contract():
    """Each persona must ask for the structured fields, or parse_opinion
    degrades every answer to caution/low and the panel is decoration."""
    for domain in SPECIALIST_DOMAINS:
        body = persona_path(domain).read_text(encoding="utf-8")
        for field in ("verdict_signal", "concerns", "questions_to_ask", "confidence"):
            assert field in body, f"{domain} persona omits {field}"
        for signal in ("blocking", "caution", "clear"):
            assert signal in body, f"{domain} persona omits the {signal} signal"


def test_the_science_personas_carry_the_vocabulary_the_rubric_lacked():
    """The audit found these words absent from every scout_hub prompt and from
    the rubric. They are the reason these two personas exist."""
    sci = persona_path("scientific").read_text(encoding="utf-8").lower()
    for term in ("control", "power", "interpretab", "translatab"):
        assert term in sci, f"scientific persona omits {term!r}"

    chem = persona_path("chemistry").read_text(encoding="utf-8").lower()
    for term in ("development candidate", "off-target", "tolerab", "selectivit"):
        assert term in chem, f"chemistry persona omits {term!r}"


def test_no_persona_claims_to_decide():
    """A specialist advises; the hub integrates. A persona that says 'reject'
    invites the hub to outsource a judgement it owns."""
    for domain in SPECIALIST_DOMAINS:
        body = persona_path(domain).read_text(encoding="utf-8").lower()
        assert "you do not decide" in body, f"{domain} persona omits the advisory boundary"
```

- [ ] **Step 2: Run to verify it fails**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -q -k persona`
Expected: FAIL — `missing persona file for scientific: prompts/specialists/scientific.md`

- [ ] **Step 3: Write the eight files**

Each file follows the same shape. Here is `prompts/specialists/scientific.md` in full — write the other seven to the same template, substituting the domain's own content from `SPECIALIST_DOMAINS`:

```markdown
# Scientific Specialist

You are the Scientific Specialist on Blackbird Laboratories' evaluation panel. The
scouting hub has asked you one question about one opportunity. Answer only within your
domain.

## What you own

Experimental rigor and whether a result can be believed:

- **Controls.** Were the right ones run? Is there a vehicle/sham/scrambled arm where the
  claim needs one?
- **Statistical power.** Is n adequate for the effect size claimed? Was the analysis
  pre-specified or found after the fact?
- **Interpretability.** Will the proposed work produce a result that is decision-enabling
  *whichever way it comes out*? A study that can only confirm is not a study.
- **Translatability.** Does the model system predict human biology? Where mouse and human
  biology diverge for this target, say so — that divergence has killed real Blackbird
  opportunities.
- **Reproducibility.** Independently replicated, or one lab one time?

## What you do not own

Commercial potential, IP, team, budget, chemistry tractability. If the question is really
about one of those, say so in one line and answer only the part that is yours.

## You do not decide

You advise. The hub integrates your opinion with seven others and owns the verdict. Do not
recommend advancing or passing; state what is and is not established, and what you would
need to see.

## Answer format

Reply with JSON and nothing else:

```
{
  "verdict_signal": "blocking | caution | clear",
  "concerns": ["one specific concern per entry"],
  "questions_to_ask": ["a question the hub should put to the PI, in the PI's language"],
  "confidence": "high | moderate | low"
}
```

- **blocking** — a flaw that makes the result unusable as it stands.
- **caution** — a real weakness that changes how much weight the result carries.
- **clear** — nothing in your domain stands in the way. Say this when it is true; a panel
  that never clears anything is noise.

`questions_to_ask` is the most valuable field you produce: it becomes the hub's next
question to the PI. Write questions a scientist would actually ask out loud, not a
checklist item.
```

The other seven differ only in `# Title`, the "What you own" bullets, and the "What you do
not own" line. Take the content for each from that domain's `owns` and `consult_when` in
`src/agent/specialists.py`, and expand each into 4-6 concrete bullets. For `chemistry`, the
bullets must cover: path to a development candidate, medchem tractability, tolerability,
in-family off-target liability, and selectivity margin. For `budget`, they must name
Blackbird's actual bands — incubation grant $300K–$847K, pre-seed $300K–$750K, seed ~$2M,
12–24 month durations — and ask whether the proposed scope fits inside one.

- [ ] **Step 4: Run the tests**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_specialists.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add prompts/specialists/ tests/unit/test_specialists.py
git commit -m "feat(specialists): the eight persona files

The scientific and chemistry personas carry the vocabulary an audit found
absent from the rubric and from every scout_hub prompt: controls, power,
interpretability, translatability, development candidate, off-target,
tolerability, selectivity. Those are the words BBL's own rejections are
written in.

Every persona states 'you do not decide' — a specialist advises and the hub
integrates. A persona that recommends passing invites the hub to outsource a
judgement it owns.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The `consult_specialist` tool

**Files:**
- Modify: `src/agent/tools.py` (`TOOL_DEFINITIONS` ends at `:116`; dispatch chain `:155-181`)
- Modify: `prompts/roles/scout_hub/role.toml`
- Test: `tests/unit/test_tool_gating.py` (append)

**Interfaces:**
- Consumes: `SPECIALIST_DOMAINS`, `persona_path`, `parse_opinion` (Task 1); the persona files (Task 2).
- Produces: `consult_specialist` in `TOOL_DEFINITIONS`; `_execute_consult_specialist(domain, question, context, *, agent_id) -> str`; an `on_consult` callback hook on `execute_tool`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tool_gating.py`:

```python
# --- consult_specialist ------------------------------------------------------

def test_consult_specialist_is_a_hub_tool_only():
    hub = {t["name"] for t in tools_for_role("scout_hub")}
    pi = {t["name"] for t in tools_for_role("pi_lab")}
    assert "consult_specialist" in hub
    assert "consult_specialist" not in pi


def test_the_tool_description_enumerates_the_eight_domains():
    """The model picks the domain from this description; if a domain is missing
    from it, that specialist is unreachable no matter what the enum allows."""
    from src.agent.specialists import SPECIALIST_DOMAINS
    from src.agent.tools import TOOL_DEFINITIONS

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "consult_specialist")
    enum = tool["input_schema"]["properties"]["domain"]["enum"]
    assert set(enum) == set(SPECIALIST_DOMAINS)
    for domain in SPECIALIST_DOMAINS:
        assert domain in tool["description"]


async def test_an_unknown_domain_is_refused_without_an_llm_call(monkeypatch):
    from src.agent import tools as tools_mod

    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(tools_mod, "generate_agent_response", _boom)
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "astrology", "question": "?", "context": ""},
        "blackbird", None, role="scout_hub",
    )
    assert "astrology" in out
    assert "scientific" in out  # names the valid domains
    assert called is False


async def test_a_missing_persona_file_does_not_report_a_consult(monkeypatch, tmp_path):
    """A persona file that isn't there must not satisfy the floor."""
    from src.agent import specialists as spec_mod
    from src.agent import tools as tools_mod

    monkeypatch.setattr(spec_mod, "SPECIALISTS_DIR", tmp_path)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": ""},
        "blackbird", None, role="scout_hub", on_consult=seen.append,
    )
    assert "legal" in out.lower()
    assert seen == []


async def test_a_successful_consult_reports_the_domain(monkeypatch):
    from src.agent import tools as tools_mod

    async def _fake(**kwargs):
        return '{"verdict_signal": "clear", "concerns": [], ' \\
               '"questions_to_ask": [], "confidence": "high"}'

    monkeypatch.setattr(tools_mod, "generate_agent_response", _fake)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": "..."},
        "blackbird", None, role="scout_hub", on_consult=seen.append,
    )
    assert seen == ["legal"]
    assert "clear" in out


async def test_a_failed_llm_call_does_not_report_a_consult(monkeypatch):
    from src.agent import tools as tools_mod

    async def _fail(**kwargs):
        raise RuntimeError("upstream 529")

    monkeypatch.setattr(tools_mod, "generate_agent_response", _fail)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": ""},
        "blackbird", None, role="scout_hub", on_consult=seen.append,
    )
    assert seen == []
    assert "error" in out.lower()


async def test_a_pi_lab_agent_cannot_consult(monkeypatch):
    from src.agent import tools as tools_mod

    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(tools_mod, "generate_agent_response", _boom)
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "?", "context": ""},
        "gill", None, role="pi_lab",
    )
    assert "not available" in out
    assert called is False
```

- [ ] **Step 2: Run to verify it fails**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_tool_gating.py -q -k consult`
Expected: FAIL — `consult_specialist` is not in `TOOL_DEFINITIONS`.

- [ ] **Step 3: Add the tool definition**

In `src/agent/tools.py`, add to the end of `TOOL_DEFINITIONS` (after the `search_prior_art`
entry, before the closing `]` at `:116`):

```python
    {
        "name": "consult_specialist",
        "description": (
            "Ask one member of the Blackbird evaluation panel for an opinion in "
            "their domain. Use this DURING an interview, as soon as the PI says "
            "something that falls in a specialist's area — their questions_to_ask "
            "become your next question to the PI, which is worth far more than "
            "asking them after you have already formed a view.\n\n"
            "Domains: 'scientific' (rigor, controls, power, interpretability, "
            "mouse-to-human translatability), 'chemistry' (path to a development "
            "candidate, medchem tractability, tolerability, in-family off-targets), "
            "'clinical' (unmet need vs standard of care, indication, patient "
            "numbers), 'commercial' (competitive landscape, named competing "
            "programs, deal comps), 'legal' (FTO, licensing, research-tool "
            "encumbrance), 'technologic' (platform feasibility, whether the work "
            "would test it), 'talent' (execution probability, conflicts of "
            "interest, over-commitment), 'budget' (scope against Blackbird's grant "
            "bands and 12-24 month durations).\n\n"
            "An advance or conditional verdict is REFUSED if the domains the idea "
            "touches were never consulted, and you cannot consult during the "
            "assessment turn — only here, in the interview. Consult early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": [
                        "scientific", "chemistry", "clinical", "commercial",
                        "legal", "technologic", "talent", "budget",
                    ],
                    "description": "Which specialist to ask.",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "The specific question, in your own words. Not 'what do you "
                        "think' — name the claim you want tested."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "The relevant part of the interview so far: what the PI "
                        "actually said about this. Quote them where you can."
                    ),
                },
            },
            "required": ["domain", "question", "context"],
        },
    },
```

- [ ] **Step 4: Add the executor and the dispatch branch**

At the top of `src/agent/tools.py`, extend the imports (keep them alphabetised — ruff `I` is on):

```python
from src.agent.specialists import (
    SPECIALIST_DOMAINS,
    parse_opinion,
    persona_path,
)
from src.services.llm import generate_agent_response
```

Add `on_consult` to `execute_tool`'s signature (after `role`):

```python
    on_consult: Callable[[str], None] | None = None,
```

and `from collections.abc import Callable` to the imports if not already present.

Add the dispatch branch immediately before the `else:` at `:181`:

```python
        elif tool_name == "consult_specialist":
            return await _execute_consult_specialist(
                tool_input["domain"],
                tool_input["question"],
                tool_input["context"],
                agent_id=agent_id,
                on_consult=on_consult,
            )
```

Add the executor beside the other `_execute_*` helpers:

```python
async def _execute_consult_specialist(
    domain: str,
    question: str,
    context: str,
    *,
    agent_id: str,
    on_consult: Callable[[str], None] | None = None,
) -> str:
    """Ask one specialist persona for an opinion.

    ``on_consult`` is invoked with the domain ONLY on a successful call. A
    refused domain, a missing persona file, or a failed LLM call must not
    satisfy the enforcement floor — otherwise "the specialist was unreachable"
    would silently become "the specialist approved".
    """
    spec = SPECIALIST_DOMAINS.get(domain)
    if spec is None:
        return (
            f"Unknown specialist domain {domain!r}. Valid domains: "
            + ", ".join(sorted(SPECIALIST_DOMAINS))
        )

    path = persona_path(domain)
    if not path.is_file():
        logger.error("[specialists] persona file missing for %s: %s", domain, path)
        return (
            f"The {domain} specialist is unavailable (persona file missing). "
            "Proceed without this opinion; it will not count as consulted."
        )

    persona = path.read_text(encoding="utf-8")
    try:
        raw = await generate_agent_response(
            system_prompt=persona,
            messages=[{
                "role": "user",
                "content": f"## Question from the hub\n\n{question}\n\n"
                           f"## What the PI has said\n\n{context}",
            }],
            max_tokens=900,
            log_meta={"agent_id": agent_id, "phase": f"consult_{domain}"},
        )
    except Exception as exc:  # noqa: BLE001 — a dead specialist must not kill the turn
        logger.error("[specialists] %s consult failed: %s", domain, exc)
        return f"Error consulting the {domain} specialist: {exc}"

    opinion = parse_opinion(raw, domain=domain)
    if on_consult is not None:
        on_consult(domain)
    logger.info(
        "[specialists] %s consulted %s -> %s (%s)",
        agent_id, domain, opinion.verdict_signal, opinion.confidence,
    )
    return f"{spec.title} — signal: {opinion.verdict_signal}\n\n{opinion.raw}"
```

- [ ] **Step 5: Add the tool to the hub's allow-list**

In `prompts/roles/scout_hub/role.toml`, extend the `tools` line:

```toml
tools = ["retrieve_profile", "retrieve_abstract", "retrieve_full_text", "search_prior_art", "consult_specialist"]
```

- [ ] **Step 6: Run the tests**

Run on the host:
`.venv-test/bin/python -m pytest tests/unit/test_tool_gating.py tests/unit/test_specialists.py tests/unit/test_roles.py -q`
Expected: PASS.

Then lint: `.venv-test/bin/python -m ruff check src/agent/tools.py tests/unit/test_tool_gating.py`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add src/agent/tools.py prompts/roles/scout_hub/role.toml tests/unit/test_tool_gating.py
git commit -m "feat(tools): consult_specialist, gated to scout_hub

on_consult fires only on a successful call. A refused domain, a missing persona
file and a failed LLM call all return a message the hub can read but none of
them reports a consult — otherwise 'the specialist was unreachable' would
silently become 'the specialist approved', which is the failure the floor
exists to prevent.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Record consults per thread

**Files:**
- Modify: `src/agent/simulation.py` (`__init__` — `_role_rate_cache` is at `:246`, `_role_post_types_cache` at `:250`; the `tool_executor` closure inside `_reply_to_thread`, above the `generate_with_tools` call at `:1373`)
- Test: `tests/unit/test_specialist_floor.py` *(new)*

**Interfaces:**
- Consumes: `on_consult` (Task 3).
- Produces: `SimulationEngine._specialist_consults: dict[str, set[str]]`, keyed by `thread_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_specialist_floor.py`:

```python
"""Consults are recorded during the phase-4 interview and read during the
separate phase-5 assessment turn. That seam — two LLM calls apart — is what the
whole enforcement floor rests on.

See docs/specs/2026-08-07-nine-evaluator-panel-design.md §4.
"""
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine(*agents):
    return SimulationEngine(agents=list(agents), slack_clients={})


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def test_the_consult_map_starts_empty():
    eng = _engine(_hub())
    assert eng._specialist_consults == {}


def test_recording_a_consult_is_keyed_by_thread():
    eng = _engine(_hub())
    eng._record_consult("t1", "chemistry")
    eng._record_consult("t1", "legal")
    eng._record_consult("t2", "scientific")
    assert eng._specialist_consults["t1"] == {"chemistry", "legal"}
    assert eng._specialist_consults["t2"] == {"scientific"}


def test_recording_the_same_domain_twice_is_idempotent():
    eng = _engine(_hub())
    eng._record_consult("t1", "chemistry")
    eng._record_consult("t1", "chemistry")
    assert eng._specialist_consults["t1"] == {"chemistry"}


def test_consults_for_an_unknown_thread_read_as_empty():
    """The floor reads this for a thread it may never have seen — after a
    restart, for instance. It must not KeyError inside _persist_assessment."""
    eng = _engine(_hub())
    assert eng._consulted_domains("never-seen") == frozenset()
```

- [ ] **Step 2: Run to verify it fails**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py -q`
Expected: FAIL — `AttributeError: 'SimulationEngine' object has no attribute '_specialist_consults'`

- [ ] **Step 3: Add the map and the two accessors**

In `src/agent/simulation.py.__init__`, beside `_role_rate_cache` (`:246`) and `_role_post_types_cache` (`:250`):

```python
        # thread_id -> the specialist domains consulted during that interview.
        # In-memory on purpose: it is read by _persist_assessment one LLM call
        # later, in the SAME process. A restart clears it, and the floor then
        # fails OPEN for threads that predate the restart — see
        # _persist_assessment. Blocking every assessment on every resumed thread
        # would be worse than one unvetted verdict.
        self._specialist_consults: dict[str, set[str]] = {}
```

Add the two accessors next to `_post_types_for_role`:

```python
    def _record_consult(self, thread_id: str, domain: str) -> None:
        """Note that a specialist was successfully consulted on this thread."""
        self._specialist_consults.setdefault(thread_id, set()).add(domain)

    def _consulted_domains(self, thread_id: str) -> frozenset[str]:
        """Domains consulted on this thread; empty for a thread we never saw."""
        return frozenset(self._specialist_consults.get(thread_id, ()))
```

- [ ] **Step 4: Wire the callback into the phase-4 tool executor**

Inside `_reply_to_thread`, find the `tool_executor` closure passed to
`generate_with_tools` (`src/agent/simulation.py:1373`) and pass `on_consult` through to
`execute_tool`, bound to this thread:

```python
            on_consult=lambda domain, _tid=thread.thread_id: self._record_consult(
                _tid, domain
            ),
```

The default-argument binding is deliberate: a bare closure over `thread` would capture the
loop variable, and this executor is built per thread.

- [ ] **Step 5: Run the tests**

Run on the host:
`.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py tests/unit/test_simulation_logic.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_specialist_floor.py
git commit -m "feat(specialists): record consults per interview thread

In-memory and keyed by thread_id. Phase 4 writes it, phase 5 reads it one LLM
call later in the same process — that seam is what the enforcement floor rests
on. A restart clears the map and the floor fails open for older threads, which
is the right trade: blocking every assessment on every resumed thread is worse
than one unvetted verdict.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: The enforcement floor

**Files:**
- Modify: `src/agent/simulation.py` (`_persist_assessment`, `:2545`)
- Test: `tests/unit/test_specialist_floor.py` (append)

**Interfaces:**
- Consumes: `required_domains_for` (Task 1), `_consulted_domains` (Task 4).
- Produces: nothing other tasks rely on.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_specialist_floor.py`:

```python
# --- the floor ---------------------------------------------------------------

import pytest


@pytest.mark.parametrize(
    "recommendation,consulted,expected_missing",
    [
        # pass and route-to-incubation never need a panel
        ("pass", set(), set()),
        ("route-to-incubation", set(), set()),
        # advance always needs scientific + talent
        ("advance", set(), {"scientific", "talent"}),
        ("advance", {"scientific"}, {"talent"}),
        ("advance", {"scientific", "talent"}, set()),
        # conditional is held to the same bar as advance
        ("conditional", set(), {"scientific", "talent"}),
        ("conditional", {"scientific", "talent"}, set()),
    ],
)
def test_floor_arithmetic(recommendation, consulted, expected_missing):
    eng = _engine(_hub())
    for d in consulted:
        eng._record_consult("t1", d)
    missing = eng._specialist_floor_gap(
        {"recommendation": recommendation}, "t1"
    )
    assert missing == expected_missing


def test_chemical_matter_pulls_chemistry_into_the_floor():
    eng = _engine(_hub())
    for d in ("scientific", "talent"):
        eng._record_consult("t1", d)
    verdict = {
        "recommendation": "advance",
        "company_or_project": "A small molecule SCAP inhibitor",
    }
    assert eng._specialist_floor_gap(verdict, "t1") == {"chemistry"}


def test_a_fully_consulted_verdict_has_no_gap():
    eng = _engine(_hub())
    for d in ("scientific", "talent", "chemistry", "clinical"):
        eng._record_consult("t1", d)
    verdict = {
        "recommendation": "advance",
        "company_or_project": "A small molecule inhibitor",
        "rationale": "for the treatment of a rare disease",
    }
    assert eng._specialist_floor_gap(verdict, "t1") == set()


def test_the_floor_fails_open_for_a_thread_we_never_saw():
    """Post-restart. An empty map must not block every assessment."""
    eng = _engine(_hub())
    verdict = {"recommendation": "advance"}
    assert eng._specialist_floor_gap(verdict, "unseen-thread") == set()


def test_fail_open_applies_only_to_a_thread_with_NO_consults():
    """A thread with one consult is a thread we DID see, so the rest are owed.
    Otherwise a hub could consult one cheap specialist and buy an exemption."""
    eng = _engine(_hub())
    eng._record_consult("t1", "budget")
    assert eng._specialist_floor_gap({"recommendation": "advance"}, "t1") == {
        "scientific", "talent",
    }
```

- [ ] **Step 2: Run to verify it fails**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py -q -k floor`
Expected: FAIL — `AttributeError: ... has no attribute '_specialist_floor_gap'`

- [ ] **Step 3: Implement the gap helper**

Add to `src/agent/simulation.py` next to `_consulted_domains`:

```python
    _PANEL_REQUIRED_FOR = frozenset({"advance", "conditional"})

    def _specialist_floor_gap(self, verdict: dict, thread_id: str | None) -> set[str]:
        """Domains this verdict was obliged to consult but did not.

        Empty means the verdict may be persisted. Only ``advance`` and
        ``conditional`` are held to the panel: a ``pass`` costs Blackbird
        nothing, so requiring eight opinions to say no would burn calls on
        exactly the ideas that do not warrant them.

        FAILS OPEN when the thread has NO recorded consults at all — that is the
        post-restart case, where the in-memory map was cleared and the interview
        genuinely happened. It does NOT fail open once any consult exists: a hub
        that consulted one cheap specialist must not thereby buy an exemption
        from the rest.
        """
        recommendation = verdict.get("recommendation")
        if recommendation not in self._PANEL_REQUIRED_FOR:
            return set()

        consulted = self._consulted_domains(thread_id or "")
        if not consulted:
            logger.info(
                "[specialists] no consult record for thread %s — floor fails open "
                "(process restarted mid-interview?)", thread_id,
            )
            return set()

        return set(required_domains_for(verdict) - consulted)
```

Add `required_domains_for` to the existing `src.agent.specialists` import at the top of the
module (create the import line if this is the first symbol used from it).

- [ ] **Step 4: Enforce it in `_persist_assessment`**

`_persist_assessment` currently begins by extracting fields from `verdict`. Add the gate at
the very top of its body, before any extraction and before the `try`, and give the method a
`thread_id` parameter:

```python
        gap = self._specialist_floor_gap(verdict, thread_id)
        if gap:
            logger.warning(
                "[%s] Assessment REFUSED for %s — recommendation %r requires the "
                "%s specialist(s), which were never consulted during the "
                "interview. Nothing persisted. Consult them in phase 4; the "
                "assessment turn has no tools.",
                agent_id, subject_hint or "?",
                verdict.get("recommendation"), ", ".join(sorted(gap)),
            )
            return
```

where `subject_hint = verdict.get("subject_agent_id")`. Thread the caller's `thread_id`
through from the phase-5 call site; when phase 5 cannot determine one (a top-level
assessment not tied to a thread) pass `None`, which makes the floor fail open by the same
rule as a restart.

- [ ] **Step 5: Run the tests**

Run on the host:
`.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py tests/integration/test_opportunity_assessment_persistence.py -q`
Expected: PASS. The integration file drives `_persist_assessment` end to end — if its
fixtures now trip the floor, that is the floor working; give those fixtures a
`recommendation` of `pass`, or record the consults they imply, rather than weakening the gate.

- [ ] **Step 6: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_specialist_floor.py \
        tests/integration/test_opportunity_assessment_persistence.py
git commit -m "feat(specialists): refuse an advance verdict whose panel was never convened

Requirements come from the verdict's own content, so a hub that skipped
Chemistry cannot also declare Chemistry unnecessary. A pass needs no panel —
requiring eight opinions to say no would burn calls on exactly the ideas that
do not warrant them.

Fails open only when a thread has NO consults at all (the post-restart case).
One consult does not buy an exemption from the rest.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Four scientific dimensions

**Files:**
- Modify: `src/services/blackbird_rubric.py:31-41`
- Modify: `tests/unit/test_blackbird_rubric.py:7-18`
- Modify: `prompts/roles/scout_hub/phase5-new-post.md` (the `scores` contract)
- Modify: `profiles/private/blackbird.md` §3 and §6 — **UNTRACKED, see below**

**Interfaces:**
- Consumes: nothing.
- Produces: a 13-key `RUBRIC_WEIGHTS`.

This task touches no file that Tasks 1-5 touch and may run in parallel with them.

- [ ] **Step 1: Update the pinning test first**

In `tests/unit/test_blackbird_rubric.py`, replace the dict in
`test_weights_match_the_pdf_and_sum_to_one_hundred` and rename it, since the PDF is no
longer the source:

```python
def test_weights_are_the_thirteen_dimensions_and_sum_to_one_hundred():
    assert RUBRIC_WEIGHTS == {
        "differentiation": 15,
        "mechanism_validation": 12,
        "market_unmet_need": 12,
        "experimental_rigor": 10,
        "toxicity_selectivity": 10,
        "team": 10,
        "chemistry_dc_path": 8,
        "external_signals": 8,
        "ip_fto": 6,
        "platform": 4,
        "dev_regulatory_feasibility": 3,
        "workplan_capital_efficiency": 1,
        "exit_thesis": 1,
    }
    assert sum(RUBRIC_WEIGHTS.values()) == 100


def test_science_carries_forty_percent():
    """BBL rejects on science. Counted over the 15 documented rejections:
    mechanism 5, toxicity 4, chemistry-to-DC 4. Before this split, a purely
    scientific objection could move at most 7 points."""
    science = {
        "mechanism_validation", "experimental_rigor",
        "toxicity_selectivity", "chemistry_dc_path",
    }
    assert sum(RUBRIC_WEIGHTS[k] for k in science) == 40
```

- [ ] **Step 2: Run to verify it fails**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_blackbird_rubric.py -q`
Expected: FAIL — the dict still has 9 keys.

- [ ] **Step 3: Update the weights**

Replace `RUBRIC_WEIGHTS` in `src/services/blackbird_rubric.py:31-41`:

```python
RUBRIC_WEIGHTS: dict[str, int] = {
    # Commercial — 60. Was 100; compressed to make room for science.
    "differentiation": 15,
    "market_unmet_need": 12,
    "team": 10,
    "external_signals": 8,
    # 10 -> 6: FTO decides 1 of the 15 documented rejections and already
    # carries a gating criterion, a red flag and a dedicated tool. Its old
    # weight double-counted an over-instrumented concern.
    "ip_fto": 6,
    "platform": 4,
    "dev_regulatory_feasibility": 3,
    "workplan_capital_efficiency": 1,
    "exit_thesis": 1,
    # Scientific — 40. New. Before this, the target-level scientific checklist
    # carried ZERO weight, so the modal BBL rejection had nowhere to land.
    "mechanism_validation": 12,   # 5 of 15 rejections
    "toxicity_selectivity": 10,   # 4 of 15
    "experimental_rigor": 10,     # the whole "what we don't want" list
    "chemistry_dc_path": 8,       # 4 of 15
}
```

`_TOTAL_WEIGHT` recomputes itself (`:45`) and `_BAND_THRESHOLDS` (`:49`) apply to the
normalised 1-5 score, so `advance`/`conditional`/`pass` keep their meanings unchanged.

- [ ] **Step 4: Update the phase-5 scores contract**

In `prompts/roles/scout_hub/phase5-new-post.md`, find the `scores` object in the
`<assessment_json>` specification and replace its key list with all thirteen, in the same
order as `RUBRIC_WEIGHTS`. Add one line beneath it:

```
Every one of the thirteen keys is required. `weighted_score` is computed server-side from
these; a key you omit scores zero, and the four scientific dimensions are 40% of the total.
```

- [ ] **Step 5: Update the private rubric — CANNOT BE COMMITTED**

`profiles/private/blackbird.md` is gitignored (`.gitignore:36`) and root-owned on the host.
Edit it over ssh, preserving ownership and mode:

```bash
ssh -o BatchMode=yes ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com \
  'cd /home/ubuntu/blackbird-copi-science && sudo cp profiles/private/blackbird.md /tmp/bb.bak'
```

Two edits:
- **§3** — add four rows to the weighted-dimension table and change `ip_fto` from 10% to 6%,
  so the table the model reads matches `RUBRIC_WEIGHTS` exactly. Each new row needs a "What
  to look for" cell: `mechanism_validation` → clinical genetic evidence, animal rescue,
  proof of mechanism, contradictory literature; `toxicity_selectivity` → on-target liability,
  in-family off-targets, therapeutic index; `experimental_rigor` → controls, power,
  interpretability, translatability; `chemistry_dc_path` → medchem tractability, path to a
  development candidate.
- **§6** — add the four keys to the `scores` skeleton.

Verify from the local mount afterwards:
`grep -c mechanism_validation profiles/private/blackbird.md` → must be ≥ 2 (table + skeleton).

- [ ] **Step 6: Run the tests**

Run on the host:
`.venv-test/bin/python -m pytest tests/unit/test_blackbird_rubric.py tests/unit/test_roles.py tests/integration/test_opportunity_assessment_persistence.py -q`
Expected: PASS. Any test asserting a 9-key `scores` object needs its fixture widened to 13 —
widen the fixture, never the production dict.

- [ ] **Step 7: Commit the tracked files**

```bash
git add src/services/blackbird_rubric.py tests/unit/test_blackbird_rubric.py \
        prompts/roles/scout_hub/phase5-new-post.md
git commit -m "feat(rubric): four scientific dimensions at 40% against 60% commercial

BBL rejects on science; the rubric scored commerce. The target-level scientific
checklist was the only place science lived and it carried zero weight, so a
purely scientific objection could move at most the 7 points of
dev_regulatory_feasibility. Counted over the 15 documented rejections:
mechanism 5, toxicity 4, chemistry-to-DC 4.

ip_fto drops 10 -> 6. It decides 1 of 15 and already carries a gating
criterion, a red flag and a dedicated tool.

No migration: scores is JSONB, _TOTAL_WEIGHT recomputes, and the band
thresholds apply to the normalised 1-5 score so advance/conditional/pass keep
their meaning. Production has zero assessment rows, so nothing is invalidated —
this is the cheapest moment the change will ever have.

profiles/private/blackbird.md §3 and §6 were updated to match but cannot be
committed: profiles/ is gitignored (.gitignore:36) since 18dfc17.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Tell the hub to use the panel

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md`
- Modify: `src/agent/thread_guidance.py` (`_SCOUT_HUB` DECIDE entry)
- Test: `tests/unit/test_thread_guidance.py` (append)

**Interfaces:**
- Consumes: the tool from Task 3, the floor from Task 5.
- Produces: nothing other tasks rely on.

`_SCOUT_HUB`'s strings are **not** snapshot-pinned — only `_PI_LAB`'s are
(`src/agent/thread_guidance.py:12-14`). Editing the scout_hub entry is allowed. Do not touch
`_PI_LAB`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_thread_guidance.py`:

```python
def test_scout_hub_decide_phase_directs_the_panel():
    _, guidance, instructions = phase4_guidance("scout_hub", 5)
    both = guidance + instructions
    assert "consult_specialist" in both
    # The two personas the rubric never had must be named explicitly, or the
    # hub will keep consulting only the ones it already thinks in terms of.
    assert "scientific" in both
    assert "chemistry" in both


def test_scout_hub_conclude_warns_that_the_floor_bites_later():
    _, guidance, instructions = phase4_guidance("scout_hub", 12)
    both = (guidance + instructions).lower()
    assert "refus" in both or "reject" in both


def test_pi_lab_guidance_is_untouched_by_the_panel():
    for count in (2, 8, 12):
        _, guidance, instructions = phase4_guidance("pi_lab", count)
        assert "consult_specialist" not in guidance + instructions
```

- [ ] **Step 2: Run to verify it fails**

Run on the host: `.venv-test/bin/python -m pytest tests/unit/test_thread_guidance.py -q`
Expected: FAIL — `consult_specialist` is absent.

- [ ] **Step 3: Extend the scout_hub DECIDE and CONCLUDE guidance**

In `src/agent/thread_guidance.py`, append to the `_SCOUT_HUB[DECIDE]` guidance string:

```
Consult the panel as you go, with consult_specialist — not at the end. Their
questions_to_ask become your next question to the PI, which is the whole value; asking
after you have formed a view wastes them. Consult `scientific` whenever the PI makes an
experimental claim and `chemistry` whenever chemical matter or a modality comes up: those
two decide most real Blackbird rejections and are the two this rubric historically had no
way to ask about.
```

And to `_SCOUT_HUB[CONCLUDE]`'s instructions string:

```
If you are heading for advance or conditional, the domains this idea touches must ALREADY
have been consulted — the assessment turn has no tools, so a verdict whose panel was never
convened is refused and nothing is persisted. If you have not consulted them by now, either
consult them in this reply or conclude at pass.
```

- [ ] **Step 4: Add the trigger text to the phase-4 template**

In `prompts/roles/scout_hub/phase4-thread-reply.md`, add a short section after the tools
list naming the eight domains and stating the floor's consequence. Keep it to eight lines —
the tool description already carries the detail, and this file is injected on every
interview turn.

- [ ] **Step 5: Run the tests and the snapshots**

Run on the host:
`.venv-test/bin/python -m pytest tests/unit/test_thread_guidance.py tests/characterization/test_agent_turn_gm.py -q`
Expected: PASS, snapshots unchanged. Verified: no test in `test_agent_turn_gm.py` builds a
`role="scout_hub"` agent, so a scout_hub-only guidance change cannot move any of them. (The
one `scout_hub` string in the `.ambr`, at `:2206`, is the `pitch` menu entry describing which
role that post type addresses — nothing to do with `_SCOUT_HUB` guidance.) **If a snapshot
moves, stop** — you have edited `_PI_LAB` or a shared string.

- [ ] **Step 6: Commit**

```bash
git add src/agent/thread_guidance.py prompts/roles/scout_hub/phase4-thread-reply.md \
        tests/unit/test_thread_guidance.py
git commit -m "feat(scout_hub): direct the hub to convene the panel during the interview

Consult early: questions_to_ask become the next question to the PI, which is
the value. Consulting after forming a view wastes them. scientific and
chemistry are named explicitly because they decide most real rejections and are
the two the rubric historically had no way to ask about.

CONCLUDE warns that the floor bites at assessment time and that the assessment
turn has no tools, so a panel not convened by then cannot be convened at all.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Full gate and deploy

**Files:** none — this changes deployment state, not the repo.

- [ ] **Step 1: The whole gate**

Run on the host: `./scripts/ci.sh`
Expected: pass. Baseline to beat: **1820 passed, 0 failed, coverage 67.79%**, `src/` ruff
**≤ 260** (it was 259 — one finding of headroom, so check the printed number).

- [ ] **Step 2: Confirm the schema is untouched**

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC exec -T postgres psql -U copi -d copi -t -A -c "SELECT version_num FROM alembic_version;"
$DC exec -T blackbird-app alembic heads
```
Expected: both `0025`. This work adds no migration; if they differ, something else is wrong.

- [ ] **Step 3: Rebuild the agent image and restart**

`src/` is baked into the agent image, so Tasks 1, 3, 4, 5, 6 and 7 all require a rebuild.
`prompts/` and `profiles/` are bind-mounted and are already live.

```bash
DC="docker compose -f docker-compose.prod.yml"
docker inspect blackbird-agent-run --format '{{index .Config.Labels "com.docker.compose.project"}}'
# MUST print copi-blackbird. If it prints copi-python, STOP — that is org1's production.

docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f
docker stop -t 30 blackbird-agent-run && docker rm blackbird-agent-run

$DC up -d --build blackbird-app worker
$DC --profile agent build agent
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

**Resume, not `--fresh`.** A `--fresh` would wipe the interview threads this panel exists to
improve. Never pass `--remove-orphans`.

- [ ] **Step 4: Verify the panel fires**

```bash
sleep 300
docker logs blackbird-agent-run 2>&1 | grep -c "\[specialists\] blackbird consulted"
docker logs blackbird-agent-run 2>&1 | grep "\[specialists\]" | tail -10
docker logs blackbird-agent-run 2>&1 | grep -c "Assessment REFUSED"
```

Expected: a non-zero consult count within a few interview turns. A `REFUSED` line is the
floor working, not a bug — but if refusals are the ONLY specialist lines, the hub is not
consulting during interviews and Task 7's guidance needs strengthening.

- [ ] **Step 5: Report**

State plainly: the consult count by domain, whether any assessment was refused and for which
domains, and whether any assessment persisted with all thirteen scores. If
`opportunity_assessments` is still empty, say so — this work does not by itself cause the
hub to produce assessments; it changes what happens when it does.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 architecture, tool not agents | 3 |
| §2 the eight specialists | 1 (table), 2 (personas) |
| §2 opinion contract | 1 (`parse_opinion`), 2 (persona format block) |
| §3 four new dimensions + weights | 6 |
| §3 blast radius (5 files) | 6 |
| §3 wiring specialist → dimension | 1 (`maps_to_dimension`), 6 |
| §4 the enforced floor | 5 |
| §4 requirement derivation table | 1 (`required_domains_for`) |
| §4 consult record | 4 |
| §5 data flow | 3, 4, 5 |
| §6 only phase 4 has tools | 3 (executor), 4 (wiring), 7 (CONCLUDE warning) |
| §7 error handling, all 8 rows | 1 (parse degradation), 3 (unknown domain, missing file, failed call), 5 (floor rows, fail-open) |
| §8 tests 1-9 | 1, 3, 4, 5, 6, 7 |
| §10 no pi_lab regression | 3 (`test_a_pi_lab_agent_cannot_consult`), 7 (`test_pi_lab_guidance_is_untouched`) |

§9 is explicitly out of scope and has no task, by design.

**Placeholder scan:** none. Task 2 is the only task that describes files rather than
printing all eight verbatim; it prints one in full as a template and specifies exactly what
the other seven must contain and which tests pin them. That is a deliberate trade against a
~2000-line plan, and the tests are the backstop.

**Type consistency:** `SpecialistSpec` fields (`domain`, `title`, `owns`, `consult_when`,
`maps_to_dimension`) are used identically in Tasks 1, 2 and 3. `parse_opinion(raw, *,
domain)` and `required_domains_for(verdict)` keep the same signatures at every call site.
`on_consult: Callable[[str], None] | None` matches between `execute_tool` (Task 3),
`_execute_consult_specialist` (Task 3) and the `lambda` in `_reply_to_thread` (Task 4).
`_record_consult(thread_id, domain)` / `_consulted_domains(thread_id)` /
`_specialist_floor_gap(verdict, thread_id)` match between Tasks 4 and 5.
`RUBRIC_WEIGHTS`' four new keys match `maps_to_dimension` in Task 1.

**Known gaps, recorded rather than hidden:**
- `_persist_assessment` gains a `thread_id` parameter in Task 5. Its existing callers must
  be updated in the same task; the integration suite is the check.
- Task 6 changes the score contract while Task 7 changes the prompt that describes it. They
  are independent files but must both land before a rebuild, or the hub emits nine keys
  against a thirteen-key rubric and scores low. Task 8's gate is the backstop.
