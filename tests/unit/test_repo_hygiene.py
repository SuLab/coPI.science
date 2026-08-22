"""Two repository-level invariants nothing else covers.

1. **`logs/` is fully ignored.** It holds run artifacts that are not source and
   are sometimes not publishable: `logs/opportunity_assessments_backup_*.sql` is
   a dump of assessment bodies and `logs/profiles_public_pre_sync_*.tgz` a
   tarball of agent profiles. `.gitignore` covered only `logs/*.json` and
   `logs/*.log`, so both were untracked **and unignored** — one `git add -A`
   away from being committed.

2. **No `src/` path can construct `ThreadDecision.outcome == "proposal"`.** The
   ✅-confirms-:memo: handshake that produced those rows was retired in Task 7 of
   `docs/plans/2026-08-12-pr34-branch2-engine-reconciliation.md`, and production
   has never held one.
   `tests/integration/test_proposal_review.py`'s module docstring states the fact
   in prose; this pins it in code, so the dead branch cannot be quietly
   resurrected — and so the still-live readers of it (`src/main.py`'s nav badge
   count, the agent dashboard's Proposals section, `src/routers/public.py`,
   `src/services/email_notifications.py`) can be retired against a proof rather
   than an assumption. See
   `docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md` (M1).
"""
import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIMULATION = ROOT / "src/agent/simulation.py"

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

    Deliberately not "argument number 3": `_close_thread`'s signature is expected
    to grow (recording which role closed the thread is a live plan item), and an
    index-based read would then silently stop looking at the outcome.
    """
    values = [*call.args, *(kw.value for kw in call.keywords)]
    return [
        v.value for v in values if isinstance(v, ast.Constant) and isinstance(v.value, str)
    ]


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
    tree = ast.parse(SIMULATION.read_text(encoding="utf-8"))

    calls = _close_thread_calls(tree)
    # The def itself is not a call, so a bare `assert calls` also pins that the
    # call sites were not all deleted out from under this test.
    assert calls, f"no _close_thread call sites found in {SIMULATION.name}"

    outcomes = {value for call in calls for value in _string_args(call)}
    assert "proposal" not in outcomes, (
        "a _close_thread call site now passes outcome='proposal'. That row type was "
        "retired with the ✅-confirms-:memo: handshake and nothing renders it "
        "correctly; if it is genuinely coming back, the dashboard/badge/public-page "
        "readers all need revisiting in the same change."
    )
    assert outcomes == LIVE_OUTCOMES, (
        f"the set of ThreadDecision outcomes src/ can produce changed to {sorted(outcomes)}. "
        "That is not necessarily wrong, but every reader that switches on `outcome` "
        "(src/main.py, src/routers/agent_page.py, src/routers/public.py, "
        "src/services/email_notifications.py) has to be checked against the new set."
    )


def test_close_thread_is_the_only_thread_decision_constructor_in_src():
    """The other half of the proof above, over all of `src/`."""
    tree = ast.parse(SIMULATION.read_text(encoding="utf-8"))
    lo, hi = _function_range(tree, "_close_thread")

    sites: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        node_tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(node_tree):
            if not isinstance(node, ast.Call) or _callee_name(node.func) != "ThreadDecision":
                continue
            rel = path.relative_to(ROOT).as_posix()
            inside = rel == SIMULATION.relative_to(ROOT).as_posix() and lo <= node.lineno <= hi
            if not inside:
                sites.append(f"{rel}:{node.lineno}")

    assert not sites, (
        f"ThreadDecision is constructed outside _close_thread at {sites} — the "
        "outcome-literal assertion in this module only covers _close_thread's call "
        "sites, so it no longer proves 'proposal' is unreachable"
    )
