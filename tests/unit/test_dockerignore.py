"""Static check that .dockerignore actually excludes the paths issue #27 I3
flagged as baked into every image layer, and does not exclude paths the
running app reads at runtime.

Docker (moby/patternmatcher) matches each pattern per path *component*, Go
filepath.Match semantics (`*`/`?` never cross `/`); a `**/` prefix means "at
any depth". This reimplements just enough of that to check the patterns in
.dockerignore — mirrors the reference Go program in
scratchpad/findings/issue_27_redteam.md §2 (not shipped in this repo).
"""

import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _patterns() -> list[str]:
    text = (REPO_ROOT / ".dockerignore").read_text()
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _pattern_matches(pattern: str, path: str) -> bool:
    any_depth = pattern.startswith("**/")
    pat_parts = pattern[3:].split("/") if any_depth else pattern.split("/")
    path_parts = path.split("/")
    width = len(pat_parts)
    starts = range(len(path_parts)) if any_depth else [0]
    for start in starts:
        window = path_parts[start : start + width]
        if len(window) != width:
            continue
        if all(fnmatch.fnmatchcase(p, pp) for p, pp in zip(window, pat_parts, strict=False)):
            return True
    return False


def _is_excluded(path: str) -> bool:
    return any(_pattern_matches(p, path) for p in _patterns())


# The I3 baked-secret/bloat paths this task closes (findings/issue_27.md I3-b..g,
# redteam NEW-1/NEW-2).
MUST_EXCLUDE = [
    "backups/prod-sync-20260810/env.prod",
    "backups/prod-sync-20260810/copi_prod_20260810.dump",
    ".venv-test/bin/python",
    "tests/unit/test_x.py",
    ".notes/cohort-system-v2.md",
    "mutants/1/src/foo.py",
    "build/lib/foo.py",
    ".hypothesis/examples/x",
    ".coverage",
    ".mutmut-cache",
    ".mypy_cache/3.11/src/main.data.json",
    "uv.lock",
    ".playwright-mcp/page.png",
    "copi.egg-info/PKG-INFO",
    ".superpowers/state.json",
    "docs/specs/2026-08-05-hub-bot-customization-design.md",
    ".env.local",
    "backups/x/.env",  # nested dotfile — belt-and-suspenders via **/.env*
    # profiles/ holds PI private profiles (profiles/private/*) — COPY . . was
    # baking them into every image layer even though prod always bind-mounts
    # the real tree over it and `migrate` (the only service without the
    # mount) never reads profiles at all (#27 I3 fix round 1).
    "profiles/public/x.md",
    "profiles/private/su.md",
    "profiles/memory/x.md",
]

# Paths the running app/worker/agent/grantbot reads from the tree at runtime —
# must stay reachable in the image (deploy_dossier.md §1 "Image contents").
# NOTE: data/, logs/ and profiles/ are deliberately NOT in this list — all
# three are excluded from the image (.dockerignore) and bind-mounted at
# runtime (docker-compose.prod.yml:95-98,124-127 for data/logs; profiles/ is
# bind-mounted on app/worker/agent/grantbot). The image never needs their
# contents, so "excluded from the image" is correct behaviour for them, not a
# bug — the Dockerfile mkdir -p's empty placeholders for the services that
# do bind-mount over them.
MUST_NOT_EXCLUDE = [
    "src/main.py",
    "prompts/profile-synthesis.md",
    "static/app.css",
    "templates/base.html",
    "alembic/env.py",
    "alembic.ini",
    "scripts/build_cabo_sankey.py",
    "requirements.lock",
    "pyproject.toml",
]


def test_dockerignore_excludes_the_i3_baked_paths():
    for path in MUST_EXCLUDE:
        assert _is_excluded(path), f"{path} should be excluded by .dockerignore but is not"


def test_dockerignore_does_not_exclude_runtime_paths():
    for path in MUST_NOT_EXCLUDE:
        assert not _is_excluded(path), f"{path} is excluded by .dockerignore but is read at runtime"
