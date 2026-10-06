"""Tests for profile versioning."""

import ast
import uuid
from pathlib import Path

from src.models.profile_revision import ProfileRevision
from src.services.profile_versioning import create_revision

SRC = Path(__file__).resolve().parents[2] / "src"

# The values a live writer may pass. "private" and "slack_dm" survive only on rows
# written before 2026-08-13; "monthly_refresh" was never written by anything.
LIVE_MECHANISMS = {
    "web", "web_impersonated", "agent", "pipeline", "grant_veto", "orcid_veto", "grant_pin",
    "reexport", "persona_sweep", "draft_accept", "paper_review", "lifecycle_export",
}
LIVE_PROFILE_TYPES = {"public", "memory"}


class TestProfileRevision:
    def test_create_revision(self):
        """ProfileRevision can be instantiated with required fields."""
        agent_id = uuid.uuid4()
        user_id = uuid.uuid4()
        rev = ProfileRevision(
            agent_registry_id=agent_id,
            profile_type="public",
            content="# Test Profile\n\nSome content.",
            changed_by_user_id=user_id,
            mechanism="web",
            change_summary="Updated research summary",
        )
        assert rev.agent_registry_id == agent_id
        assert rev.profile_type == "public"
        assert rev.content == "# Test Profile\n\nSome content."
        assert rev.changed_by_user_id == user_id
        assert rev.mechanism == "web"
        assert rev.change_summary == "Updated research summary"

    def test_create_revision_agent_initiated(self):
        """Agent-initiated revisions have no changed_by_user_id."""
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="memory",
            content="Working memory content.",
            mechanism="agent",
            change_summary="Updated after thread closure: su <> wiseman",
        )
        assert rev.changed_by_user_id is None
        assert rev.mechanism == "agent"
        assert rev.profile_type == "memory"

    def test_create_revision_pipeline(self):
        """Pipeline-generated revisions have mechanism='pipeline' and no user."""
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="public",
            content="Generated profile content.",
            mechanism="pipeline",
            change_summary="Profile generated from ORCID + PubMed",
        )
        assert rev.changed_by_user_id is None
        assert rev.mechanism == "pipeline"

    def test_a_historical_private_slack_dm_row_still_loads(self):
        """A pre-2026-08-13 private/slack_dm row still maps; nothing writes one now."""
        pi_id = uuid.uuid4()
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="private",
            content="Updated private profile.",
            changed_by_user_id=pi_id,
            mechanism="slack_dm",
            change_summary="PI instruction: prioritize aging collaborations",
        )
        assert rev.changed_by_user_id == pi_id
        assert rev.mechanism == "slack_dm"
        assert rev.profile_type == "private"

    def test_nullable_change_summary(self):
        """change_summary can be None."""
        rev = ProfileRevision(
            agent_registry_id=uuid.uuid4(),
            profile_type="public",
            content="Content.",
            mechanism="web",
        )
        assert rev.change_summary is None

    def test_repr(self):
        agent_id = uuid.uuid4()
        rev = ProfileRevision(
            agent_registry_id=agent_id,
            profile_type="public",
            content="Content.",
            mechanism="web",
        )
        r = repr(rev)
        assert "public" in r
        assert "web" in r


def _is_create_revision_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "create_revision") or (
        isinstance(func, ast.Attribute) and func.attr == "create_revision"
    )


def _loop_values(name: str, call: ast.Call, parents: dict) -> set | None:
    """Constants a loop variable takes, when an enclosing ``for`` binds ``name``.

    Handles ``for name in ("a", "b")`` and ``for name, other in [("a", x), ...]``
    over a literal list/tuple. Returns None when the binding is not of that shape.
    """
    node = parents.get(call)
    while node is not None:
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.List, ast.Tuple)):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                elts = node.iter.elts
                if all(isinstance(e, ast.Constant) for e in elts):
                    return {e.value for e in elts}
                return None
            if isinstance(target, ast.Tuple):
                positions = [
                    i for i, t in enumerate(target.elts)
                    if isinstance(t, ast.Name) and t.id == name
                ]
                if positions:
                    i = positions[0]
                    values = set()
                    for e in node.iter.elts:
                        if not (
                            isinstance(e, (ast.Tuple, ast.List))
                            and len(e.elts) == len(target.elts)
                            and isinstance(e.elts[i], ast.Constant)
                        ):
                            return None
                        values.add(e.elts[i].value)
                    return values
        node = parents.get(node)
    return None


def _constant_values(node: ast.expr) -> set | None:
    """A literal, or a conditional choosing between literals."""
    if isinstance(node, ast.Constant):
        return {node.value}
    if isinstance(node, ast.IfExp):
        a, b = _constant_values(node.body), _constant_values(node.orelse)
        return None if a is None or b is None else a | b
    return None


def _enclosing_function(node: ast.AST, parents: dict):
    node = parents.get(node)
    while node is not None and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        node = parents.get(node)
    return node


def _src_trees() -> list[tuple[ast.Module, dict]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        out.append((tree, {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}))
    return out


def _forwarded_values(func_name: str, param: str, seen: frozenset = frozenset()) -> set | None:
    """Every value `param` of `func_name` takes across src/: its default when a
    caller omits it, each caller's literal, and, recursively, what a caller that
    forwards one of its own parameters receives. None marks a non-literal source.
    A forwarded None means "no revision" (export_and_record), so it is dropped."""
    if (func_name, param) in seen:
        return None
    seen = seen | {(func_name, param)}
    values: set = set()
    default = None
    trees = _src_trees()
    for tree, _parents in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                args = node.args.kwonlyargs
                for a, d in zip(args, node.args.kw_defaults, strict=True):
                    if a.arg == param and d is not None:
                        default = _constant_values(d)
                pos = node.args.args[len(node.args.args) - len(node.args.defaults):]
                for a, d in zip(pos, node.args.defaults, strict=True):
                    if a.arg == param:
                        default = _constant_values(d)
    for tree, parents in trees:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == func_name):
                continue
            kw = next((k for k in node.keywords if k.arg == param), None)
            if kw is None:
                if default is None:
                    return None
                values |= default
                continue
            literal = _constant_values(kw.value)
            if literal is not None:
                values |= literal
                continue
            outer = _enclosing_function(node, parents)
            if not (isinstance(kw.value, ast.Name) and outer is not None):
                return None
            forwarded = _forwarded_values(outer.name, kw.value.id, seen)
            if forwarded is None:
                return None
            values |= forwarded
    return values - {None}


def _argument_values(call: ast.Call, keyword: str, parents: dict) -> set | None:
    for kw in call.keywords:
        if kw.arg != keyword:
            continue
        literal = _constant_values(kw.value)
        if literal is not None:
            return literal
        if isinstance(kw.value, ast.Name):
            looped = _loop_values(kw.value.id, call, parents)
            if looped is not None:
                return looped
            outer = _enclosing_function(call, parents)
            if outer is not None and kw.value.id in {
                a.arg for a in outer.args.args + outer.args.kwonlyargs
            }:
                # A wrapper forwarding its own parameter (profile_publish.export_and_record):
                # check what every caller passes it, transitively.
                return _forwarded_values(outer.name, kw.value.id)
        return None
    return None


def test_live_writers_use_only_live_values():
    """Every create_revision call in src/ passes a live mechanism and profile type.

    Each value must be a literal, or a loop variable over a literal tuple (the CLI
    backfill), so a historical value cannot creep back in through a writer.
    """
    calls = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if _is_create_revision_call(node):
                calls.append((path.relative_to(SRC.parent), node, parents))

    # Six call sites existed when this guard was written; Phase 3 (RB-09) folded the
    # four profile-export writers into profile_publish.export_and_record, whose
    # forwarded mechanism is checked through its callers. Finding fewer would mean
    # the scan, not the writers, had changed.
    assert len(calls) >= 3, [str(p) for p, _, _ in calls]
    assert any(str(p) == "src/services/profile_publish.py" for p, _, _ in calls)
    assert any(str(p) == "src/cli.py" for p, _, _ in calls)

    for path, call, parents in calls:
        where = f"{path}:{call.lineno}"
        mechanisms = _argument_values(call, "mechanism", parents)
        assert mechanisms is not None, f"{where}: mechanism is not a literal"
        assert mechanisms <= LIVE_MECHANISMS, f"{where}: {mechanisms}"
        profile_types = _argument_values(call, "profile_type", parents)
        assert profile_types is not None, f"{where}: profile_type is not a literal"
        assert profile_types <= LIVE_PROFILE_TYPES, f"{where}: {profile_types}"


def test_create_revision_docstring_names_every_live_value():
    doc = create_revision.__doc__
    assert doc is not None
    for value in LIVE_MECHANISMS | LIVE_PROFILE_TYPES:
        assert f'"{value}"' in doc, value


def test_every_mechanism_fits_the_column():
    """profile_revisions.mechanism is String(20); spec 2026-10-05 §6.1 lists eleven; Phase 3
    adds paper_review."""
    spec = {
        "pipeline", "web", "web_impersonated", "agent", "grant_veto", "orcid_veto", "grant_pin",
        "draft_accept", "reexport", "persona_sweep", "lifecycle_export", "paper_review",
    }
    assert LIVE_MECHANISMS <= spec and all(len(m) <= 20 for m in spec)
