"""Which code is this process actually running?

The agent image bakes ``src/`` at build time (see docs/operations/host-and-simulation.md:
"The agent image does NOT mount src/"), so the truthful answer to "what commit is running" is
the commit the IMAGE was built from, not whatever the host repo says at launch.
Two sources, in order:

1. ``.build_info.json`` at the repo/image root — written by
   ``scripts/write_build_info.py`` during the Docker build, the only moment a
   git binary is available (``python:3.11-slim`` ships none). Carries a
   ``dirty_files`` count, which the fallback below cannot know.
2. A pure-Python read of ``.git/HEAD`` + loose refs + ``packed-refs``. This
   serves only processes running from a host checkout: images never carry
   ``.git`` (the Dockerfile's builder stage deletes it after writing the JSON),
   so inside an image a missing JSON degrades to "unavailable". Every read
   below is guarded, so neither source's absence raises.

Dependency-free on purpose: imported by the simulation engine at startup and
by tests that never touch a database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: src/services/build_info.py -> parents[2] is the repo root on the host and
#: /app inside the image — the two layouts are identical by construction.
REPO_ROOT = Path(__file__).resolve().parents[2]

BUILD_INFO_FILENAME = ".build_info.json"


@dataclass(frozen=True)
class BuildInfo:
    commit: str | None
    branch: str | None
    #: Count of modified TRACKED files at image build time (``git status
    #: --porcelain --untracked-files=no | wc -l``). None when only the .git
    #: fallback was available — the dirty state is unknowable without git.
    dirty_files: int | None
    source: str  # "build_info_json" | "git_dir" | "unavailable"


def _from_json(root: Path) -> BuildInfo | None:
    path = root / BUILD_INFO_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    commit = data.get("commit")
    branch = data.get("branch")
    dirty = data.get("dirty_files")
    return BuildInfo(
        commit=str(commit) if commit else None,
        branch=str(branch) if branch else None,
        dirty_files=int(dirty) if isinstance(dirty, int) else None,
        source="build_info_json",
    )


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    loose = git_dir / ref
    try:
        return loose.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        pass
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            # "<sha> <refname>"; '#' comments and '^' peel lines are skipped.
            if line.startswith(("#", "^")) or " " not in line:
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha.strip() or None
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _from_git_dir(root: Path) -> BuildInfo | None:
    git_dir = root / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ").strip()
        # removeprefix, not rsplit: branch names may contain '/', e.g. feat/x.
        branch = ref.removeprefix("refs/heads/")
        return BuildInfo(_resolve_ref(git_dir, ref), branch, None, "git_dir")
    if head:  # detached HEAD: the line IS the sha
        return BuildInfo(head, None, None, "git_dir")
    return None


def get_build_info(root: Path | None = None) -> BuildInfo:
    """Never raises: an unreadable identity degrades to 'unavailable'."""
    root = root or REPO_ROOT
    info = _from_json(root)
    if info is None:
        info = _from_git_dir(root)
    if info is None:
        info = BuildInfo(None, None, None, "unavailable")
    logger.debug("build info served from %s", info.source)
    return info


#: What `SimulationRun.total_api_calls` counts, said where an operator will read
#: it. The column is rendered to humans in three admin templates, and on
#: 2026-08-22 its UNITS changed: `Agent.record_api_call` plus
#: `SimulationEngine._unbooked_calls` now book every real API call (tool rounds
#: and truncation retries included) where the column used to count turns, and the
#: restart rebuild moved with it (`_CALLS_PER_LOG_ROW`). 78.6% of stored
#: `thread_reply` rows are 2+ calls, so the number roughly doubles for reasons
#: that have nothing to do with the run.
#:
#: This sits beside the rubric version/hash line for the same reason that one
#: exists: a change of meaning that is invisible at read time is a change nobody
#: can correct for afterwards.
API_CALL_UNITS_NOTE = (
    "API-call accounting: SimulationRun.total_api_calls counts REAL API CALLS "
    "(tool rounds and truncation retries included), not turns, as of "
    "2026-08-22. It is NOT comparable with any run recorded before that date. "
    "The old per-turn figure is recoverable for any run as "
    "SELECT COUNT(*) FROM llm_call_logs WHERE simulation_run_id = <run>."
)
