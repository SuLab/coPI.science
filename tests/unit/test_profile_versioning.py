"""Tests for profile versioning."""

import ast
import uuid
from pathlib import Path

from src.models.profile_revision import ProfileRevision
from src.services.profile_versioning import create_revision

SRC = Path(__file__).resolve().parents[2] / "src"

# The values a live writer may pass. "private" and "slack_dm" survive only on rows
# written before 2026-08-13; "monthly_refresh" was never written by anything.
LIVE_MECHANISMS = {"web", "agent", "pipeline"}
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


def _argument_values(call: ast.Call, keyword: str, parents: dict) -> set | None:
    for kw in call.keywords:
        if kw.arg != keyword:
            continue
        if isinstance(kw.value, ast.Constant):
            return {kw.value.value}
        if isinstance(kw.value, ast.Name):
            return _loop_values(kw.value.id, call, parents)
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

    # Six call sites existed when this guard was written; finding none would mean
    # the scan, not the writers, had changed.
    assert len(calls) >= 6, [str(p) for p, _, _ in calls]
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
