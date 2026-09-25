"""Every citation of the cohort spec resolves to a numbered heading that exists.

Comments across the tree cite `specs/cohort-system-v2.md` as `v2 §N` or
`cohort-system-v2 §N` (older ones say `.notes/cohort-system-v2.md §N`, the path the spec
was written at). Two of them cited `§9.4`, a section the spec never had; the intended
target was item 4 of §9's **Requirements.** list. Nothing checked, so the drift was found
only by reading. This test parses the real spec and fails on any citation that does not
resolve.

Grammar: `§N` (N dotted) must name a parsed section; `§N req. M` must name a section
whose body carries a `**Requirements.**` list with an item `M.`. Several references may
share one prefix, separated by `/` or `,` (`v2 §9 req. 4 / §13.1`).

Scanned: `src/`, `tests/`, `templates/`, `scripts/`, `alembic/`, `specs/` and the top-level
`docs/*.md`. Not scanned: `docs/audits/`, `docs/plans/` and `docs/specs/`, which are dated
records that quote defective citations verbatim, and this file, whose synthetic strings
are deliberately unresolvable.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "specs" / "cohort-system-v2.md"
THIS = Path(__file__).resolve()

SCAN_DIRS = ("src", "tests", "templates", "scripts", "alembic", "specs")
SUFFIXES = {".py", ".md", ".html", ".sh", ".toml", ".txt", ".yml", ".yaml"}

REF = r"§\d+(?:\.\d+)*(?:\s+req\.\s*\d+)?"
CITE = re.compile(r"(?:cohort-system-v2(?:\.md)?`*\s*|\bv2\s+)(" + REF + r"(?:\s*[/,]\s*" + REF + r")*)")
ONE = re.compile(r"§(\d+(?:\.\d+)*)(?:\s+req\.\s*(\d+))?")

_HEADING = re.compile(r"^#{1,6}\s")
_NUMBERED = re.compile(r"^#{1,6}\s+(\d+(?:\.\d+)*)\.?\s")
_REQUIREMENTS = "**Requirements.**"


def parse_spec(text: str) -> dict[str, list[str]]:
    """Map each numbered section to its body lines.

    A line starting with three backticks toggles a code fence; headings inside a fence
    (the spec's config example has `# Cohort interaction gate`) are body text. A numbered
    heading opens its section; any other heading (the appendices) closes the current one.
    A section's body stops at the next heading of any level, so §9's body excludes §9.1.
    """
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            if current is not None:
                current.append(line)
            continue
        if not in_fence and _HEADING.match(line):
            m = _NUMBERED.match(line)
            current = sections.setdefault(m.group(1), []) if m else None
            continue
        if current is not None:
            current.append(line)
    return sections


def resolves(sections: dict[str, list[str]], number: str, req: str | None) -> bool:
    body = sections.get(number)
    if body is None:
        return False
    if req is None:
        return True
    if not any(_REQUIREMENTS in line for line in body):
        return False
    item = re.compile(rf"^{re.escape(req)}\.\s")
    return any(item.match(line) for line in body)


def references(text: str) -> list[tuple[str, str | None]]:
    """Every (section, requirement-or-None) cited in `text`."""
    out: list[tuple[str, str | None]] = []
    for cite in CITE.finditer(text):
        out.extend((m.group(1), m.group(2)) for m in ONE.finditer(cite.group(1)))
    return out


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        files.extend(p for p in (ROOT / d).rglob("*") if p.is_file() and p.suffix in SUFFIXES)
    files.extend(p for p in (ROOT / "docs").glob("*.md") if p.is_file())
    return sorted(p for p in files if p.resolve() != THIS and "__pycache__" not in p.parts)


def _citations() -> list[tuple[str, int, str, str | None]]:
    """(relative path, line number, section, requirement) for every citation in the tree."""
    found: list[tuple[str, int, str, str | None]] = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for number, req in references(line):
                found.append((rel, lineno, number, req))
    return found


def _sections() -> dict[str, list[str]]:
    return parse_spec(SPEC.read_text(encoding="utf-8"))


def test_the_spec_parser_sees_numbered_sections():
    assert {"5.1", "6.3.1", "9", "14.6"} <= set(_sections())


def test_the_scanner_finds_the_known_citations():
    found = _citations()
    assert len(found) > 30, f"only {len(found)} citations found; the scanner has gone blind"
    assert any(rel == "src/services/cohorts.py" and number == "5.3" for rel, _, number, _ in found)


def test_citation_grammar():
    sections = _sections()

    def resolved(text: str) -> list[bool]:
        refs = references(text)
        assert refs, f"no reference parsed from {text!r}"
        return [resolves(sections, n, r) for n, r in refs]

    assert resolved("see v2 §9 req. 4") == [True]
    assert resolved("see v2 §9 req. 5") == [False]
    assert resolved("see v2 §9.4") == [False]
    assert resolved("see specs/cohort-system-v2.md §4.2 / §14.4") == [True, True]


def test_every_cohort_spec_citation_resolves():
    sections = _sections()
    failures = [
        f"{rel}:{lineno} §{number}" + (f" req. {req}" if req else "")
        for rel, lineno, number, req in _citations()
        if not resolves(sections, number, req)
    ]
    assert failures == [], (
        "these cohort-spec citations name no numbered heading (or requirement) in "
        "specs/cohort-system-v2.md; cite a numbered requirement as `§N req. M`:\n"
        + "\n".join(failures)
    )
