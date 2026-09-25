"""Every mutation-harness target still names exactly one site in the tree.

Each `scripts/mutate_*.sh` applier refuses a target that occurs zero times or more than
once and reports ERROR, so a harness whose anchors drifted cannot run at all — and nothing
noticed until someone ran it. Three anchors had drifted that way (M12b's docstring was
rewritten; cohorts M4 and slack-mirror S1 each matched several lines). This test parses
each script's `MUTANTS=(` array the way bash would and counts every target in the file it
names, so the drift fails CI instead.

For `mutate_system.sh` it also checks the tier tables: every tier a mutant uses is
declared, and every tier with a real mutant has an INERT control, in the same tier or in
the tier `INERT_COVERS` names for it (a control is only meaningful over a selection that
contains the real mutant's).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

# script -> number of `~~` fields per entry (mutate_system carries a leading tier).
LAYOUT = {"mutate_system.sh": 5, "mutate_cohorts.sh": 4, "mutate_slack_mirror.sh": 4}

VACUITY_TIERS = (
    "vac_i20",
    "vac_i23_route",
    "vac_i23_pass",
    "vac_c2",
    "vac_i21",
    "vac_i28",
    "vac_i29",
    "vac_i24b",
)

# A tier whose inert control lives in another tier. M12b, the pubmed inert control, lives
# in `pubmed_both`, whose `-k` selects both single-test pubmed tiers; M13 lives in
# `vacuity`, the union of every `vac_*` node. Each premise is asserted below.
INERT_COVERS = {
    "pubmed_doi": "pubmed_both",
    "pubmed_tool": "pubmed_both",
    **{t: "vacuity" for t in VACUITY_TIERS},
}


def _array_lines(text: str, opener: str) -> list[str]:
    """The raw lines between `opener` and the closing `)` line, blank and `#` lines dropped."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == opener)
    out: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped == ")":
            return out
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    raise AssertionError(f"{opener} is never closed")


def _entries(script: str) -> list[tuple[str, list[str]]]:
    """(raw line, fields) for each MUTANTS entry, with `\\n` turned into a newline."""
    raw = _array_lines((SCRIPTS / script).read_text(encoding="utf-8"), "MUTANTS=(")
    out = []
    for line in raw:
        tokens = shlex.split(line)
        assert len(tokens) == 1, f"{script}: entry is not one bash word: {line!r}"
        fields = tokens[0].split("~~")
        assert len(fields) == LAYOUT[script], f"{script}: wrong field count: {line!r}"
        out.append((line, fields))
    return out


def _target(script: str, fields: list[str]) -> tuple[str, str, str]:
    """(file, from, label) of one entry, normalised as the appliers normalise it."""
    if LAYOUT[script] == 5:
        _, file, frm, _, label = fields
    else:
        file, frm, _, label = fields
    return file, frm.replace("\\n", "\n"), label


def _assoc(text: str, name: str) -> dict[str, str]:
    """A `declare -A name=( [key]="value" ... )` table, values unquoted as bash would."""
    lines = _array_lines(text, f"declare -A {name}=(")
    table: dict[str, str] = {}
    for line in lines:
        for key, value in re.findall(r"\[(\w+)\]=(\"(?:[^\"\\]|\\.)*\"|\S*)", line):
            (table[key],) = shlex.split(value) or [""]
    return table


def _system_tables() -> tuple[dict[str, str], dict[str, str]]:
    text = (SCRIPTS / "mutate_system.sh").read_text(encoding="utf-8")
    return _assoc(text, "TIER_SELECT"), _assoc(text, "TIER_CREDS")


def _paths(select: str) -> list[str]:
    """The node/file arguments of a selection, `-k EXPR` dropped."""
    argv = shlex.split(select)
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "-k":
            skip = True
        else:
            out.append(arg)
    return out


def _k_expr(select: str) -> str | None:
    argv = shlex.split(select)
    return argv[argv.index("-k") + 1] if "-k" in argv else None


@pytest.mark.parametrize("script", sorted(LAYOUT))
def test_every_target_occurs_exactly_once(script):
    entries = _entries(script)
    assert entries, f"{script}: no MUTANTS entries parsed; the parser has gone blind"
    wrong = []
    for _, fields in entries:
        file, frm, label = _target(script, fields)
        count = (ROOT / file).read_text(encoding="utf-8").count(frm)
        if count != 1:
            wrong.append(f"{label.split()[0]}: {file} has {count} occurrences of {frm!r}")
    assert wrong == [], f"{script}: re-point these targets:\n" + "\n".join(wrong)


@pytest.mark.parametrize("script", sorted(LAYOUT))
def test_no_double_quoted_entry_expands(script):
    # shlex expands neither, but bash does: `$x` is a parameter and a backtick in double
    # quotes is command substitution. Such an entry must be single-quoted.
    bad = [
        line for line, _ in _entries(script) if line.startswith('"') and ("$" in line or "`" in line)
    ]
    assert bad == [], f"{script}: single-quote these entries:\n" + "\n".join(bad)


def test_every_mutant_tier_is_declared():
    select, creds = _system_tables()
    tiers = {fields[0] for _, fields in _entries("mutate_system.sh")}
    assert tiers <= set(select), f"undeclared tiers: {tiers - set(select)}"
    # Under `set -u` a tier missing from TIER_CREDS aborts the run mid-way.
    assert set(select) == set(creds), set(select) ^ set(creds)
    assert all(creds[t] == "" for t in (*VACUITY_TIERS, "vacuity"))


def test_every_tier_with_a_real_mutant_has_an_inert_control():
    inert_tiers, real_tiers = set(), set()
    for _, fields in _entries("mutate_system.sh"):
        tier, label = fields[0], fields[4]
        (inert_tiers if "INERT" in label else real_tiers).add(tier)
    uncovered = {
        t for t in real_tiers if t not in inert_tiers and INERT_COVERS.get(t) not in inert_tiers
    }
    assert uncovered == set(), f"tiers with no inert control: {sorted(uncovered)}"


def test_inert_covers_premises_hold():
    select, _ = _system_tables()
    # pubmed_both's -k names both single-test pubmed selections.
    both = _k_expr(select["pubmed_both"])
    assert both is not None
    for tier in ("pubmed_doi", "pubmed_tool"):
        name = _k_expr(select[tier])
        assert name and name in both, f"pubmed_both does not select {tier}'s test"
    # vacuity selects (at least) every vac_* node, and narrows none of them.
    assert _k_expr(select["vacuity"]) is None
    union = set(_paths(select["vacuity"]))
    for tier in VACUITY_TIERS:
        nodes = _paths(select[tier])
        assert len(nodes) == 1, f"{tier} must select exactly one node, got {nodes}"
        assert "::" in nodes[0], f"{tier} selects a whole file, not its fixed test's node"
        assert nodes[0] in union, f"vacuity does not select {tier}'s node {nodes[0]}"
