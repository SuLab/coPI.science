"""Three repository-level invariants nothing else covers.

1. **`logs/` is fully ignored.** It holds run artifacts that are not source and
   are sometimes not publishable: `logs/opportunity_assessments_backup_*.sql` is
   a dump of assessment bodies and `logs/profiles_public_pre_sync_*.tgz` a
   tarball of agent profiles. `.gitignore` covered only `logs/*.json` and
   `logs/*.log`, so both were untracked **and unignored** — one `git add -A`
   away from being committed.

2. **No `src/` path can construct `ThreadDecision.outcome == "proposal"`.** The
   ✅-confirms-:memo: handshake that produced those rows was retired by the
   pitch-only reconciliation
   (`docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md` §8), and
   production has never held one.
   `tests/integration/test_proposal_review.py`'s module docstring states the fact
   in prose; this pins it in code, so the dead branch cannot be quietly
   resurrected. Its old readers (the nav badge count, the agent dashboard's
   Proposals section, public voting and the notification emails) were retired on
   2026-09-29 against this proof. See
   `docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md` (M1).

3. **`static/` is source, not ignored.** Every file under it is tracked and
   served by the app, but `.gitignore` listed `static/` as a generated bundle, so
   a new asset stayed untracked unless someone forced it in with `git add -f`.
   See `docs/audits/2026-09-24-comment-cleanup-rca/README.md` (I36).
"""
import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIMULATION = ROOT / "src/agent/simulation.py"
ENGINE_SOURCES = [SIMULATION, *sorted((ROOT / "src/agent/engine").glob("*.py"))]
THREADS = ROOT / "src/agent/engine/threads.py"

# The only two outcomes any live code path produces. Not a wish-list: both are
# driven for real by tests/integration/test_proposal_review.py.
LIVE_OUTCOMES = {"timeout", "no_proposal"}


# ---------------------------------------------------------------------------
# 1. .gitignore
# ---------------------------------------------------------------------------

def _check_ignore(relpath: str) -> int:
    return subprocess.run(
        ["git", "check-ignore", "-q", "--", relpath],
        cwd=ROOT, capture_output=True,
    ).returncode


def test_logs_directory_is_fully_ignored():
    """Every path under `logs/`, whatever its extension. The two that motivated
    this are named explicitly because they are the ones carrying data: a SQL dump
    of `opportunity_assessments` and a tarball of `profiles/public`."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git work tree")

    # Control first: an obviously tracked path must NOT be reported ignored, so a
    # `git check-ignore` that succeeded for everything (or failed for everything)
    # cannot make the assertions below pass.
    assert _check_ignore("src/main.py") == 1, (
        "git check-ignore reports src/main.py as ignored — the probe itself is broken, "
        "so the logs/ assertions below prove nothing"
    )

    for relpath in (
        "logs/opportunity_assessments_backup_1787265062.sql",
        "logs/profiles_public_pre_sync_1786656614.tgz",
        "logs/blackbird_run_1787391032.log",
        "logs/nested/anything.tar.gz",
    ):
        assert _check_ignore(relpath) == 0, (
            f"{relpath} is not gitignored — `git add -A` would commit it. `logs/` "
            "holds run artifacts (including dumps of assessment bodies and agent "
            "profiles) and must be ignored wholesale, not per-extension."
        )


def test_static_sources_are_not_ignored():
    """A new file under `static/` must be addable with plain `git add`."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git work tree")

    assert _check_ignore("src/main.py") == 1, (
        "git check-ignore reports src/main.py as ignored — the probe itself is broken"
    )
    for relpath in ("static/js/new_asset.js", "static/css/new.css"):
        assert _check_ignore(relpath) == 1, (
            f"{relpath} is gitignored — `static/` holds tracked source the app serves, "
            "so a new asset would silently stay out of every commit and every image"
        )


# ---------------------------------------------------------------------------
# 2. outcome='proposal' is unreachable
# ---------------------------------------------------------------------------

def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _close_thread_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_name(node.func) == "_close_thread"
    ]


def _string_args(call: ast.Call) -> list[str]:
    """Every string literal passed at a call site, positional or keyword.

    Deliberately wider than the outcome parameter for the "proposal" check below:
    if that literal reappears anywhere in a `_close_thread` call it is worth a
    failure regardless of which parameter it landed in.
    """
    values = [*call.args, *(kw.value for kw in call.keywords)]
    return [
        v.value for v in values if isinstance(v, ast.Constant) and isinstance(v.value, str)
    ]


def _outcome_arg(call: ast.Call) -> str | None:
    """The `outcome` argument specifically, or None if it is not a literal.

    Narrower than `_string_args` on purpose: `_close_thread`'s signature has
    already grown (`closed_by_role` records which role closed the thread) and
    may grow again, and a set-equality assertion fed by every literal at the
    call site would fail on an unrelated new argument.
    """
    for kw in call.keywords:
        if kw.arg == "outcome":
            value = kw.value
            break
    else:
        # `self` is bound, so (agent, thread, outcome) puts outcome at index 2.
        if len(call.args) <= 2:
            return None
        value = call.args[2]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _function_range(tree: ast.AST, name: str) -> tuple[int, int]:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node.lineno, node.end_lineno or node.lineno
    raise AssertionError(f"{name} no longer exists in {SIMULATION.name}")


def test_no_src_path_can_construct_a_proposal_outcome():
    """The outcome string reaches the DB only through `_close_thread`, and no call
    site passes `"proposal"`.

    Two assertions, because either alone is weak: the literals actually passed
    (which is where a resurrection would appear), and the fact that
    `_close_thread` is still the sole `ThreadDecision(...)` constructor in `src/`
    (without which a new constructor elsewhere could reintroduce the row while
    this test stayed green).
    """
    calls = [
        c for p in ENGINE_SOURCES for c in _close_thread_calls(ast.parse(p.read_text(encoding="utf-8")))
    ]
    # The def itself is not a call, so a bare `assert calls` also pins that the
    # call sites were not all deleted out from under this test.
    assert calls, f"no _close_thread call sites found in {SIMULATION.name}"

    literals = {value for call in calls for value in _string_args(call)}
    assert "proposal" not in literals, (
        "a _close_thread call site now passes the literal 'proposal'. That row type "
        "was retired with the ✅-confirms-:memo: handshake and nothing renders it "
        "correctly; if it is genuinely coming back, the dashboard/badge/public-page "
        "readers all need revisiting in the same change."
    )

    outcomes = {_outcome_arg(call) for call in calls}
    assert None not in outcomes, (
        "a _close_thread call site passes a non-literal outcome, so this test can no "
        "longer enumerate what src/ produces — read the call sites by hand"
    )
    assert outcomes == LIVE_OUTCOMES, (
        f"the set of ThreadDecision outcomes src/ can produce changed to {sorted(outcomes)}. "
        "That is not necessarily wrong, but every reader that switches on `outcome` "
        "(src/services/directory.py, src/routers/admin.py, src/agent/simulation.py, "
        "src/agent/agent.py) has to be checked against the new set."
    )


# `_close_thread` hands its row to `_record_decision`, which writes it through
# `_write_decision` at once or queues it on `_pending_decisions` for
# `flush_pending_decisions` to write later (S2-06, S1-12). The row is built in
# `_close_thread` and never rebuilt, so `_write_decision` may construct the model
# as long as nothing else can reach that path.
_DECISION_WRITE_PATH = {
    "_write_decision": {"_record_decision", "flush_pending_decisions"},
    "_record_decision": {"_close_thread"},
    "_pending_decisions": {"__init__", "_record_decision", "flush_pending_decisions"},
}


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map each node id to the name of the innermost function holding it."""
    owner: dict[int, str] = {}

    def _visit(node: ast.AST, fn: str | None) -> None:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            fn = node.name
        if fn is not None:
            owner[id(node)] = fn
        for child in ast.iter_child_nodes(node):
            _visit(child, fn)

    _visit(tree, None)
    return owner


def test_close_thread_is_the_only_thread_decision_constructor_in_src():
    """The other half of the proof above, over all of `src/`: every
    `ThreadDecision(...)` is either inside `_close_thread` or in `_write_decision`,
    whose rows only ever come from `_close_thread`."""
    tree = ast.parse(THREADS.read_text(encoding="utf-8"))
    ranges = {name: _function_range(tree, name) for name in ("_close_thread", "_write_decision")}
    threads_rel = THREADS.relative_to(ROOT).as_posix()

    sites: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        node_tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(node_tree):
            if not isinstance(node, ast.Call) or _callee_name(node.func) != "ThreadDecision":
                continue
            rel = path.relative_to(ROOT).as_posix()
            inside = rel == threads_rel and any(
                lo <= node.lineno <= hi for lo, hi in ranges.values()
            )
            if not inside:
                sites.append(f"{rel}:{node.lineno}")

    assert not sites, (
        f"ThreadDecision is constructed outside _close_thread at {sites} — the "
        "outcome-literal assertion in this module only covers _close_thread's call "
        "sites, so it no longer proves 'proposal' is unreachable"
    )


def test_the_decision_write_path_only_carries_close_threads_rows():
    """`_write_decision` builds a ThreadDecision from a row dict, so the proof above
    holds only if every row it writes is one `_close_thread` built with its own
    `outcome` parameter. Pin that: each name on the write path is touched only by
    its expected functions, anywhere in `src/`, and `_close_thread` passes
    `"outcome": outcome` to `_record_decision`."""
    touched: dict[str, set[str]] = {name: set() for name in _DECISION_WRITE_PATH}
    for path in sorted((ROOT / "src").rglob("*.py")):
        node_tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(node_tree)
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(node_tree):
            name = node.attr if isinstance(node, ast.Attribute) else (
                node.id if isinstance(node, ast.Name) else None
            )
            if name in touched:
                fn = owner.get(id(node), "<module>")
                touched[name].add(fn if path == THREADS else f"{rel}:{fn}")

    for name, allowed in _DECISION_WRITE_PATH.items():
        assert touched[name] <= allowed, (
            f"{name} is now reached from {sorted(touched[name] - allowed)}; the "
            "ThreadDecision rows it carries may no longer all come from _close_thread"
        )

    tree = ast.parse(THREADS.read_text(encoding="utf-8"))
    close = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_close_thread"
    )
    assert "outcome" in [a.arg for a in close.args.args]
    records = [
        n for n in ast.walk(close)
        if isinstance(n, ast.Call) and _callee_name(n.func) == "_record_decision"
    ]
    assert len(records) == 1, "expected exactly one _record_decision call in _close_thread"
    (row,) = [a for a in records[0].args if isinstance(a, ast.Dict)]
    outcome = [
        v for k, v in zip(row.keys, row.values, strict=True)
        if isinstance(k, ast.Constant) and k.value == "outcome"
    ]
    assert len(outcome) == 1 and isinstance(outcome[0], ast.Name) and outcome[0].id == "outcome", (
        "_close_thread no longer passes its own `outcome` parameter as the row's "
        "outcome, so the literal enumeration over _close_thread call sites no longer "
        "covers what the decision write path stores"
    )
