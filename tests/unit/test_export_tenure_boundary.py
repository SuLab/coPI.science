"""Tripwire + behavioural tests for the tenure-scoping boundary at
``export_profile_to_markdown``.

Task B10 of ``docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md``
(§3B D16, §5 Task 10): two of eight call sites tenure-filtered their
publications before calling ``export_profile_to_markdown``, six did not. The
fix moved the filter inside the function boundary — ``publications`` must now
be a ``TenureScopedPublications`` (``src/services/tenure_scope.py``), and the
function raises ``TypeError`` on anything else, including a bare list. This
file has two jobs:

1. A static AST walk over every ``export_profile_to_markdown(`` call site in
   ``src/`` and ``scripts/`` asserting each one obtains its ``publications``
   argument from a ``tenure_scope`` producer (``scoped_publications_for_export``
   or ``scope_for_export``), not from a raw ``select(Publication)``/list
   expression. A written comment is what failed before (two call sites'
   filters were hand-copied, drifted, and six more never got the memo at
   all) — a tripwire is what is needed instead.
2. Behavioural tests on the boundary itself: a tenure year drops pre-tenure
   rows, ``tenure_start=None`` is a full-career pass-through (D20), and a bare
   list is refused with ``TypeError``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.services.profile_export import export_profile_to_markdown
from src.services.tenure_scope import TenureScopedPublications, scope_for_export

REPO_ROOT = Path(__file__).resolve().parents[2]

# The producers a call site's `publications=` argument is allowed to trace
# back to. Anything else (a bare `select(Publication)` result, a hand-rolled
# list comprehension) must not reach this boundary — that shape is exactly
# what produced six of the eight misses.
_ALLOWED_PRODUCERS = {"scoped_publications_for_export", "scope_for_export"}

# The exact call sites this task converted. A ninth caller — or the loss of
# one of these eight — must fail this test loudly rather than silently
# widening or narrowing the audited set.
_EXPECTED_CALL_SITES = {
    ("src", "routers", "agent_page.py"),
    ("src", "routers", "onboarding.py"),
    ("src", "routers", "manager.py"),
    ("src", "services", "profile_pipeline.py"),
    ("src", "services", "profile_edit.py"),
    ("scripts", "audit_pub_dois.py"),
    ("scripts", "backfill_agents.py"),
    ("scripts", "generate_sparsedata_user.py"),
}


def _iter_source_files():
    for root in ("src", "scripts"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _producer_name(node: ast.expr) -> str | None:
    """Best-effort: the name of the call or variable an argument traces to.

    This is intentionally a single-hop lookup, not a full dataflow analysis
    — it names the function called (for `x = scoped_publications_for_export(...)`
    followed by `publications=x`) or, if the argument is itself a call
    expression, its callee's name directly.
    """
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _find_call_sites() -> dict[tuple[str, ...], list[ast.Call]]:
    sites: dict[tuple[str, ...], list[ast.Call]] = {}
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "export_profile_to_markdown"
            ):
                calls.append(node)
            # `export_profile_to_markdown`'s own def (in profile_export.py) is
            # a FunctionDef, not a Call, so the match above never counts it.
        rel = path.relative_to(REPO_ROOT).parts
        if calls:
            sites[rel] = calls
    return sites


def _assigned_producer_names(tree: ast.Module) -> dict[str, str]:
    """Map local variable name -> producer function name, for simple
    `var = producer(...)` assignments anywhere in the module (function-scope
    shadowing is not modeled; these are small linear functions)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                name = _producer_name(node.value)
                if name:
                    out[target.id] = name
    return out


def test_export_call_site_count_is_exactly_eight():
    """A ninth caller (or fewer than eight) must be reviewed, not silently
    absorbed — this is the count named in the remediation plan and audit."""
    sites = _find_call_sites()
    found = set(sites.keys())
    assert found == _EXPECTED_CALL_SITES, (
        f"export_profile_to_markdown call sites changed.\n"
        f"missing: {_EXPECTED_CALL_SITES - found}\n"
        f"new/unexpected: {found - _EXPECTED_CALL_SITES}\n"
        "Every call site must obtain `publications` from "
        "src.services.tenure_scope; review and update this test's "
        "_EXPECTED_CALL_SITES deliberately if the count really changed."
    )


def test_every_call_site_passes_a_tenure_scoped_producer():
    sites = _find_call_sites()
    for rel_parts, calls in sites.items():
        path = REPO_ROOT.joinpath(*rel_parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        local_producers = _assigned_producer_names(tree)
        for call in calls:
            pub_arg = None
            for kw in call.keywords:
                if kw.arg == "publications":
                    pub_arg = kw.value
                    break
            if pub_arg is None and len(call.args) >= 4:
                pub_arg = call.args[3]
            assert pub_arg is not None, (
                f"{'/'.join(rel_parts)}: export_profile_to_markdown call at "
                f"line {call.lineno} passes no `publications` argument at all"
            )
            name = _producer_name(pub_arg)
            if name not in _ALLOWED_PRODUCERS and isinstance(pub_arg, ast.Name):
                name = local_producers.get(pub_arg.id)
            assert name in _ALLOWED_PRODUCERS, (
                f"{'/'.join(rel_parts)}:{call.lineno}: publications= does not "
                f"trace to scoped_publications_for_export/scope_for_export "
                f"(resolved producer: {name!r}). A bare list or raw query "
                "result at this boundary is exactly the bug this test guards "
                "against."
            )


# --- Behavioural tests on the boundary itself -------------------------------


class _FakeUser:
    name = "Test PI"
    institution = None
    department = None


class _FakeProfile:
    research_summary = None
    techniques = None
    experimental_models = None
    disease_areas = None
    key_targets = None
    keywords = None
    grant_titles = None


class _FakePub:
    def __init__(self, year, title="A Paper"):
        self.year = year
        self.title = title
        self.journal = None
        self.doi = None
        self.pmid = None


def test_bare_list_is_refused_with_type_error():
    with pytest.raises(TypeError, match="scoped_publications_for_export"):
        export_profile_to_markdown(
            _FakeUser(), _FakeProfile(), "test-agent", publications=[_FakePub(2020)]
        )


def test_none_publications_is_still_accepted(tmp_path, monkeypatch):
    import src.services.profile_export as profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    path = export_profile_to_markdown(_FakeUser(), _FakeProfile(), "test-agent", publications=None)
    assert path is not None


def test_tenure_scoped_export_drops_pre_tenure_publications(tmp_path, monkeypatch):
    import src.services.profile_export as profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pubs = [_FakePub(2015, "Old Paper"), _FakePub(2022, "New Paper")]
    scoped = scope_for_export(pubs, tenure_start=2020)
    assert isinstance(scoped, TenureScopedPublications)
    path = export_profile_to_markdown(
        _FakeUser(), _FakeProfile(), "test-agent", publications=scoped
    )
    content = path.read_text(encoding="utf-8")
    assert "New Paper" in content
    assert "Old Paper" not in content


def test_tenure_start_none_is_a_full_career_pass_through(tmp_path, monkeypatch):
    import src.services.profile_export as profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pubs = [_FakePub(2001, "Ancient Paper"), _FakePub(2022, "New Paper")]
    scoped = scope_for_export(pubs, tenure_start=None)
    path = export_profile_to_markdown(
        _FakeUser(), _FakeProfile(), "test-agent", publications=scoped
    )
    content = path.read_text(encoding="utf-8")
    assert "Ancient Paper" in content
    assert "New Paper" in content
