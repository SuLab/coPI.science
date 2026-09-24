"""No module may carry its own copy of the PI author/affiliation matcher.

The 2026-09-22 audit's D2/D12 defects had already been "fixed" once — in
``src/services/corpus.py`` — while ``scripts/generate_sparsedata_user.py``
kept private copies of ``_author_first_name_matches``, ``_aff_match``,
``_distinctive_aff_tokens`` and ``INSTITUTION_STOPWORDS`` with the OLD
behaviour. That script seeded 56 of the 73 production PIs and is still
runnable, so re-running it would have reintroduced every defect the service
fix removed.

A second copy is invisible to the tests that pin the first one, which is why
this is a tripwire rather than a comment. The rule: exactly ONE definition of
each, in ``src/services/corpus.py``; everyone else imports it.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CANONICAL = REPO / "src" / "services" / "corpus.py"

# Names whose behaviour the corpus identity gate depends on. A redefinition
# anywhere else is a silent fork of the matcher.
SINGLE_DEFINITION_NAMES = {
    "_author_first_name_matches",
    "_aff_match",
    "_distinctive_aff_tokens",
    "INSTITUTION_STOPWORDS",
    "match_pi_author",
}

SEARCH_ROOTS = ("src", "scripts")


def _definitions_in(path: Path) -> set[str]:
    """Module-level function/assignment names defined in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                t.id for t in node.targets if isinstance(t, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_the_matcher_has_exactly_one_definition_per_name():
    offenders: dict[str, list[str]] = {}
    for root in SEARCH_ROOTS:
        for path in (REPO / root).rglob("*.py"):
            if path == CANONICAL or "__pycache__" in path.parts:
                continue
            for name in _definitions_in(path) & SINGLE_DEFINITION_NAMES:
                offenders.setdefault(name, []).append(
                    str(path.relative_to(REPO))
                )
    assert offenders == {}, (
        "these files redefine matcher internals instead of importing them "
        f"from src/services/corpus.py: {offenders}. A private copy does not "
        "get fixed when the service does — that is exactly how the D2/D12 "
        "defects survived in scripts/generate_sparsedata_user.py."
    )


def test_the_canonical_module_really_defines_them():
    # Guards the test above against silently passing if corpus.py is renamed
    # or the names change: an empty search set would make it vacuous.
    assert SINGLE_DEFINITION_NAMES <= _definitions_in(CANONICAL)
