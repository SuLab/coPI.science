"""`docs/audits/open-findings.md` is a register, not prose (RCA §8 cause 6).

S1 was rated CRITICAL on 2026-08-17 and was still open five weeks later because
an audit's findings lived only in its own prose. The register fixes that only if
it stays machine-checkable: every row well formed, every `fixed` row backed by
evidence that resolves, and every finding of the 2026-08-17 final infra audit
present.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "audits" / "open-findings.md"
INFRA_AUDIT = ROOT / "docs" / "audits" / "2026-08-17-user-account-types" / "final-infra-audit.md"

HEADER = ("id", "source", "severity", "status", "evidence", "owner / decision")
STATUSES = {"open", "fixed", "refuted", "deferred"}
# An owner cell that names nobody.
_PLACEHOLDERS = {"", "-", "—", "?", "tbd", "none", "n/a"}

_BACKTICKED = re.compile(r"`([^`]+)`")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_PATH_LINE = re.compile(r"^([\w./-]+):(\d+)(?:-(\d+))?$")


def _rows(text: str) -> list[list[str]]:
    """The register table's data rows, as stripped cells. Parsing starts at the
    header row, so the rules list above the table is never read as data."""
    lines = text.splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if tuple(c.strip() for c in line.strip().strip("|").split("|")) == HEADER
    )
    rows = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def _row_problems(row: list[str]) -> list[str]:
    if len(row) != len(HEADER):
        return [f"{len(row)} cells, want {len(HEADER)}"]
    ident, _source, _severity, status, _evidence, owner = row
    problems = []
    if not ident:
        problems.append("empty id")
    if status not in STATUSES:
        problems.append(f"status {status!r} not in {sorted(STATUSES)}")
    if status in {"open", "deferred"} and owner.lower() in _PLACEHOLDERS:
        problems.append("open/deferred row names no owner or decision")
    return problems


def _has_git(root: Path) -> bool:
    return (root / ".git").exists()


def _evidence_problems(evidence: str, root: Path) -> list[str]:
    """For a `fixed` row: at least one backticked commit or `path:line`, and
    every one of them must resolve. Other backticked text (image tags, host
    paths) is quoted context and is not checked."""
    refs = 0
    problems = []
    for token in _BACKTICKED.findall(evidence):
        if _COMMIT.match(token):
            refs += 1
            if _has_git(root):
                found = subprocess.run(
                    ["git", "-C", str(root), "cat-file", "-e", f"{token}^{{commit}}"],
                    capture_output=True,
                ).returncode == 0
                if not found:
                    problems.append(f"commit {token} not found")
            continue
        m = _PATH_LINE.match(token)
        if m:
            refs += 1
            target = root / m.group(1)
            line = int(m.group(3) or m.group(2))
            if not target.is_file():
                problems.append(f"{token}: no such file")
            elif line > len(target.read_text(encoding="utf-8", errors="replace").splitlines()):
                problems.append(f"{token}: past the end of the file")
    if refs == 0:
        problems.append("no backticked commit or path:line")
    return problems


def _register_rows() -> list[list[str]]:
    rows = _rows(REGISTER.read_text(encoding="utf-8"))
    assert len(rows) > 5, f"control: the register parsed only {len(rows)} rows"
    return rows


def test_every_row_is_well_formed():
    bad = {row[0] if row else "?": p for row in _register_rows() if (p := _row_problems(row))}
    assert not bad, f"malformed register rows: {bad}"
    ids = [row[0] for row in _register_rows()]
    assert len(ids) == len(set(ids)), "duplicate register ids"


def test_fixed_rows_cite_real_evidence():
    fixed = [row for row in _register_rows() if len(row) == len(HEADER) and row[3] == "fixed"]
    bad = {row[0]: p for row in fixed if (p := _evidence_problems(row[4], ROOT))}
    assert not bad, f"`fixed` rows whose evidence does not resolve: {bad}"


def _infra_audit_ids() -> list[str]:
    text = INFRA_AUDIT.read_text(encoding="utf-8")
    start = text.index("\n## Findings")
    # Search past the heading itself, or `^## ` matches "## Findings" at start + 1.
    end = re.compile(r"^## ", re.M).search(text, start + len("\n## Findings"))
    section = text[start : end.start() if end else len(text)]
    return re.findall(r"^### ([CHML]\d+) — ", section, re.M)


def test_the_2026_08_17_infra_audit_is_fully_registered():
    audit_ids = _infra_audit_ids()
    assert len(audit_ids) == 12, f"control: expected 12 findings, parsed {audit_ids}"
    registered = {row[0] for row in _register_rows()}
    missing = [i for i in audit_ids if f"2026-08-17/{i}" not in registered]
    assert not missing, f"2026-08-17 infra-audit findings missing from the register: {missing}"


@pytest.mark.parametrize(
    "row, expect",
    [
        (["x/1", "s", "low", "open", "e", "owner"], []),
        (["x/1", "s", "low", "closed", "e", "owner"], ["status"]),
        (["x/1", "s", "low", "open", "e", "—"], ["owner"]),
        (["x/1", "s", "low", "deferred", "e", ""], ["owner"]),
        (["x/1", "s", "low", "open", "e"], ["cells"]),
    ],
)
def test_row_validator_controls(row, expect):
    problems = _row_problems(row)
    assert len(problems) == len(expect)
    for problem, word in zip(problems, expect, strict=True):
        assert word in problem


def test_evidence_validator_controls():
    assert _evidence_problems("see `CLAUDE.md:1`", ROOT) == []
    assert _evidence_problems("host fact only, `copi-blackbird-app:latest`", ROOT) == [
        "no backticked commit or path:line"
    ]
    assert _evidence_problems("`src/nope.py:1`", ROOT) == ["src/nope.py:1: no such file"]
    assert _evidence_problems("`CLAUDE.md:99999999`", ROOT) == [
        "CLAUDE.md:99999999: past the end of the file"
    ]
    if _has_git(ROOT):
        assert _evidence_problems("`0000000`", ROOT) == ["commit 0000000 not found"]
