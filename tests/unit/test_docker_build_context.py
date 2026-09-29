"""The image holds the tracked tree, never the checkout (RCA S1).

Before `.dockerignore` and the two-stage Dockerfile, `COPY . .` baked the whole
host checkout into every image: `.env`, the production dumps under `backups/`,
`.git`, `.venv-test`. Two layers now keep that out, and this module pins both
without needing Docker:

1. `.dockerignore` keeps secrets and production data out of the build context,
   so they never reach the daemon or a cache layer. It may name only UNTRACKED
   paths: the builder stage counts dirty files with `git status
   --untracked-files=no`, where an excluded tracked path would look deleted.
2. The Dockerfile's `source` stage runs `git clean -ffdx`, writes
   `.build_info.json` and deletes `.git` in one RUN. Every final-stage COPY,
   the pip layer's `pyproject.toml` and `src/` included, takes from `source`,
   so no untracked file the deny-list misses can reach the image.
"""
import fnmatch
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = ROOT / ".dockerignore"
DOCKERFILE = ROOT / "Dockerfile"

MUST_EXCLUDE = (
    ".env*", "backups", "data", "logs", "profiles", ".venv-test",
    ".superpowers", ".claude", "certbot", ".provision_state.json",
)

# Paths the image reads (or the builder stage needs, for `.git`). No positive
# pattern may name or glob any of them.
MUST_KEEP = (
    ".git", "src", "templates", "static", "prompts", "scripts", "alembic",
    "alembic.ini", "pyproject.toml",
)


def _patterns() -> tuple[list[str], list[str]]:
    """(positive, negated) patterns, with any leading `/` or `./` stripped."""
    positive: list[str] = []
    negated: list[str] = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        target = negated if line.startswith("!") else positive
        body = line.lstrip("!").strip()
        body = body.removeprefix("./").lstrip("/")
        target.append(body)
    return positive, negated


def _raw_negation_lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("!")
    ]


def test_dockerignore_excludes_secrets_and_production_data():
    assert DOCKERIGNORE.is_file(), (
        ".dockerignore is missing: every build context carries .env, backups/ "
        "(production dumps) and .venv-test to the daemon"
    )
    positive, _ = _patterns()
    missing = [p for p in MUST_EXCLUDE if p not in positive]
    assert not missing, f".dockerignore no longer excludes {missing}"
    # A presence check is only sound if nothing later re-includes what these
    # lines exclude, so the one permitted negation is pinned exactly.
    assert _raw_negation_lines() == ["!.env.example"], (
        "the only negation .dockerignore may carry is `!.env.example`; another `!` "
        f"line could re-include an excluded secret: {_raw_negation_lines()}"
    )


def test_dockerignore_keeps_what_the_image_reads():
    positive, _ = _patterns()
    offenders = []
    for pattern in positive:
        bare = pattern.rstrip("/")
        if bare in ("*", "**"):
            offenders.append(pattern)
            continue
        for name in MUST_KEEP:
            if bare == name or fnmatch.fnmatch(name, bare) or fnmatch.fnmatch(
                f"{name}/probe", bare
            ):
                offenders.append(f"{pattern} (matches {name})")
    assert not offenders, (
        f".dockerignore excludes paths the image or builder stage needs: {offenders}"
    )


def _tracked(pattern: str) -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", f":(glob){pattern}", f":(glob){pattern}/**"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return {p for p in out.split("\0") if p}


def test_dockerignore_excludes_only_untracked_paths():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git work tree")
    positive, negated = _patterns()
    excluded: set[str] = set()
    for pattern in positive:
        excluded |= _tracked(pattern)
    for pattern in negated:
        excluded -= _tracked(pattern)
    assert not excluded, (
        f".dockerignore excludes tracked paths {sorted(excluded)}. The builder "
        "stage's `git status --untracked-files=no` would count each as deleted, "
        "inflating dirty_files in every image, and the image would lack them."
    )


def _instructions() -> list[tuple[str, str]]:
    """(KEYWORD, arguments) per Dockerfile instruction, continuations joined."""
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if pending and line.startswith("#"):
            continue  # a comment inside a continued instruction
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        full = (pending + line).strip()
        pending = ""
        keyword, _, args = full.partition(" ")
        instructions.append((keyword.upper(), args.strip()))
    if pending.strip():
        keyword, _, args = pending.strip().partition(" ")
        instructions.append((keyword.upper(), args.strip()))
    return instructions


def _stages() -> list[tuple[str | None, list[tuple[str, str]]]]:
    """(stage name or None, instructions) per FROM, in file order."""
    stages: list[tuple[str | None, list[tuple[str, str]]]] = []
    for keyword, args in _instructions():
        if keyword == "FROM":
            words = args.split()
            name = words[2] if len(words) >= 3 and words[1].upper() == "AS" else None
            stages.append((name, []))
        elif stages:
            stages[-1][1].append((keyword, args))
    return stages


def test_final_stage_never_copies_the_build_context():
    stages = _stages()
    assert len(stages) >= 2, "the Dockerfile is single-stage: it bakes the build context"

    final_name, final = stages[-1]
    assert not [a for k, a in final if k == "ADD"], "the final stage uses ADD"
    for keyword, args in final:
        if keyword != "COPY":
            continue
        words = shlex.split(args)
        flags = [w for w in words if w.startswith("--")]
        paths = [w for w in words if not w.startswith("--")]
        # Every final-stage COPY, the pip layer included, must take from the cleaned
        # `source` stage: a context COPY skips `git clean`, so an untracked file under
        # src/ would reach /app/src and site-packages.
        assert "--from=source" in flags, (
            f"final-stage `COPY {args}` takes from the build context. Only "
            "`COPY --from=source …` may: anything else can bake untracked checkout "
            "files into the image."
        )
        assert len(paths) == 2, f"final-stage `COPY {args}`: expected one source, one dest"

    source = [body for name, body in stages if name == "source"]
    assert len(source) == 1, "there must be exactly one stage named `source`"
    steps = ("git clean -ffdx", "write_build_info.py", "rm -rf .git")
    runs = [a for k, a in source[0] if k == "RUN" and any(s in a for s in steps)]
    assert len(runs) == 1, (
        "the source stage must clean, write .build_info.json and delete .git in ONE "
        f"RUN; found {runs}"
    )
    positions = [runs[0].find(s) for s in steps]
    assert -1 not in positions and positions == sorted(positions), (
        f"the source stage's RUN must contain {steps} in that order: {runs[0]!r}"
    )
