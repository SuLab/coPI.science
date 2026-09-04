#!/usr/bin/env python3
"""Verify requirements.lock is consistent with pyproject.toml — deterministically.

WHY NOT "does the lock equal a fresh pip-compile?"
=================================================
That was the first implementation of this gate (#27 I4-e), and it cannot work as a
gate, because it compares the committed lock against *whatever PyPI holds right now*:

  * Any of the ~200 transitive packages publishing a release makes the committed lock
    differ from a fresh resolve, with no change to this repository. Measured on
    2026-09-04: `alembic 1.19.2` was published at 17:10:12Z and the gate went red for
    every developer within the hour, blaming "pyproject.toml changed without
    regenerating it" — which nobody had done.
  * It is not even reproducible within one machine. Two back-to-back runs of the same
    `pip-compile` command disagreed (alembic 1.19.1 vs 1.19.2, rich 14.3.4 vs 15.0.0)
    depending on which HTTP cache the ephemeral environment saw.

`scripts/ci.sh` is the whole gate and the pre-push hook runs it (decision D17), so a
check that goes red on its own schedule does not protect the lock — it just stops
people pushing, and teaches them to reach for LOCKCHECK=none.

WHAT THIS CHECKS INSTEAD
========================
The failure the gate actually needs to catch is drift *inside the repository*: someone
edits a dependency in pyproject.toml and does not regenerate the lock. That is fully
determined by the two files, so it is checked from the two files:

  1. every direct dependency in `[project].dependencies` appears in the lock, and
  2. the version the lock pins satisfies the specifier pyproject declares.

Adding, removing, or re-constraining a dependency without regenerating therefore fails,
immediately and for a reason that names the package. Upstream releasing something new
does not.

The freshness question — "could this lock be newer?" — is a maintenance task, not a
merge blocker; `LOCK_SMOKE=1` proves the lock installs and imports, and regenerating is
one documented command. `LOCKCHECK=strict` in ci.sh still runs the old fresh-resolve
comparison for anyone who wants it deliberately.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

# pip-compile emits extras verbatim ("uvicorn[standard]==0.38.0"), so the bracket group
# is part of the line and must be stripped rather than breaking the match.
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s\;]+)")


def parse_lock(text: str) -> dict[str, str]:
    """canonical name -> pinned version, from a pip-compile --generate-hashes lock."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith((" ", "\t", "#", "-")):
            continue                      # hash continuations, comments, pip options
        m = _PIN.match(line.strip())
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    return pins


def direct_requirements(pyproject: dict) -> list[Requirement]:
    return [Requirement(spec) for spec in pyproject.get("project", {}).get("dependencies", [])]


def check(pyproject_path: Path, lock_path: Path) -> list[str]:
    with pyproject_path.open("rb") as fh:
        pyproject = tomllib.load(fh)
    pins = parse_lock(lock_path.read_text())
    problems: list[str] = []

    if not pins:
        return [f"{lock_path} contains no `name==version` pins — is it a lockfile?"]

    for req in direct_requirements(pyproject):
        name = canonicalize_name(req.name)
        pinned = pins.get(name)
        if pinned is None:
            problems.append(
                f"{req.name}: declared in pyproject.toml but absent from the lock — "
                "the lock was not regenerated after this dependency was added"
            )
            continue
        if not req.specifier:
            continue
        try:
            version = Version(pinned)
        except InvalidVersion:
            problems.append(f"{req.name}: lock pins {pinned!r}, which is not a valid version")
            continue
        # prereleases=True: a lock legitimately pins one if the specifier allows it.
        if not req.specifier.contains(version, prereleases=True):
            problems.append(
                f"{req.name}: pyproject declares {str(req.specifier)!r} but the lock pins "
                f"{pinned} — regenerate the lock (see the command in scripts/ci.sh)"
            )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pyproject", default="pyproject.toml")
    ap.add_argument("--lock", default="requirements.lock")
    args = ap.parse_args()

    problems = check(Path(args.pyproject), Path(args.lock))
    if problems:
        print("ERROR: requirements.lock does not match pyproject.toml:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nRegenerate with:\n"
            "  uv run --isolated --no-project --python 3.11 --with pip-tools -- \\\n"
            "    python -m piptools compile --generate-hashes --no-header "
            "-o requirements.lock pyproject.toml",
            file=sys.stderr,
        )
        return 1

    with Path(args.pyproject).open("rb") as fh:
        n = len(direct_requirements(tomllib.load(fh)))
    print(f"    {n} direct dependencies, every pin satisfies pyproject's specifier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
