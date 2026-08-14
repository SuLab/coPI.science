"""`slack_sdk` may be imported in exactly two modules.

Fix 4 centralised pagination, retry and splitting inside AgentSlackClient, and
8515f65 then found the same four defects still live in files that had built their
own WebClient. A chokepoint you can walk around is not a chokepoint, so this test
is the wall. Adding a third importer is a design decision; make it deliberately
by editing ALLOWED, not accidentally by writing `from slack_sdk import WebClient`
in a route.
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

ALLOWED = {
    "agent/slack_client.py",   # the engine's chokepoint
    "services/slack_web.py",   # the web/service boundary
}

# Known limitation, stated so nobody mistakes this for airtight: the check is
# static and line-based, so `importlib.import_module("slack_sdk")` or
# `__import__` would slip past it. Neither appears anywhere in src/ today
# (verified), and a dynamic import of a transport is odd enough to notice in
# review. What this test does buy is that the ordinary way to bypass the
# boundary — writing `from slack_sdk import WebClient` in a route — is a build
# failure rather than a defect found in production.

_IMPORT = re.compile(r"^\s*(?:from\s+slack_sdk[.\w]*\s+import|import\s+slack_sdk)", re.M)


def _importers() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        lines = [
            i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if _IMPORT.match(line)
        ]
        if lines:
            found[rel] = lines
    return found


def test_slack_sdk_is_imported_only_at_the_two_boundaries():
    extra = {k: v for k, v in _importers().items() if k not in ALLOWED}
    assert not extra, (
        "these modules import slack_sdk directly, bypassing the boundary — route "
        f"them through src.services.slack_web instead: {extra}"
    )


def test_both_allowed_boundaries_still_exist():
    """Guard against the invariant passing because a boundary was deleted."""
    importers = _importers()
    for allowed in ALLOWED:
        assert allowed in importers, (
            f"{allowed} no longer imports slack_sdk — if it was removed, remove it "
            "from ALLOWED too so this test keeps meaning something"
        )
