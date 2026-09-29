"""The runbook's repo references must resolve (RCA §8 cause 4: prose that nothing reads).

The operator runbook is the root CLAUDE.md, the nested CLAUDE.md files Claude Code
loads when it reads their directory (`NESTED`), and the `docs/operations/` pages they
point at. Outside the sync-tested anchors nothing read it, so deploy boxes, file paths
and `path:N` citations drifted silently as the tree moved underneath them. This module
checks the mechanically checkable part:

- every "Deploy order for `00NN_slug`" box names a migration that exists;
- every backticked path under one of `REPO_PREFIXES` exists (brace groups
  expanded, `*` treated as a glob, `/**` as "this directory exists");
- every `path:N` / `path:N-M` citation is within the cited file's length;
- every CLAUDE.md stays under the recommended length, and the root one indexes
  every operations page.

It cannot tell whether a cited line still says what the prose claims; it only
catches references that point at nothing.
"""
import glob
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = ROOT / "CLAUDE.md"
#: Nested CLAUDE.md files. `test_every_claude_md_is_listed` keeps this complete.
NESTED = (ROOT / "src" / "agent" / "CLAUDE.md", ROOT / "alembic" / "CLAUDE.md")
OPERATIONS = ROOT / "docs" / "operations"
#: https://code.claude.com/docs/en/memory, "Write effective instructions": "target
#: under 200 lines per CLAUDE.md file. Longer files consume more context and reduce
#: adherence."
MAX_CLAUDE_MD_LINES = 200

REPO_PREFIXES = ("src", "scripts", "tests", "templates", "prompts", "alembic", "docs", "specs")

# Repo paths the runbook names on purpose although they do not exist in a clean
# checkout, each with the reason. Keep it empty unless a path is deliberately
# untracked or removed; a stale entry fails `test_allowlist_entries_are_needed`.
ALLOWLIST: dict[str, str] = {}

# An optional leading `./` is consumed, so `./scripts/ci.sh` is checked as
# `scripts/ci.sh`; any other `.` or `/` before the prefix means a longer path.
_PATH_TOKEN = re.compile(
    r"(?<![\w./~$-])(?:\./)?((?:" + "|".join(REPO_PREFIXES) + r")/"
    r"(?:[^\s`'\"(),;<>{}]|\{[^{}\s`]*\})*)"
)
# A top-level file cited with a line number, e.g. `Dockerfile:41`.
_ROOT_FILE_LINE = re.compile(r"(?<![\w./~$-])([A-Za-z][\w.-]*):(\d+)(?:-(\d+))?\b")
_LINE_SUFFIX = re.compile(r"^(.*?):(\d+)(?:-(\d+))?$")
_DEPLOY_BOX = re.compile(r"Deploy order for `(\d{4}_\w+)`")


def _runbooks() -> list[Path]:
    return [CLAUDE_MD, *NESTED, *sorted(OPERATIONS.glob("*.md"))]


def _texts() -> dict[str, str]:
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in _runbooks()}


def _without_fences(text: str) -> str:
    """Drop fenced code blocks (also inside `>` quote boxes): their contents are
    commands, not backticked references, and their ``` markers would mis-pair the
    inline backticks."""
    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip("> ").startswith("```"):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else line)
    return "\n".join(out)


def _spans(text: str) -> list[str]:
    """Inline backtick spans, with a line-wrap (and its `>` quote prefix) folded
    to one space."""
    return [
        re.sub(r"\n[>\s]*", " ", m.group(1))
        for m in re.finditer(r"`([^`]+)`", _without_fences(text))
    ]


def _expand_braces(path: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", path)
    if not m:
        return [path]
    return [
        expanded
        for alt in m.group(1).split(",")
        for expanded in _expand_braces(path[: m.start()] + alt + path[m.end():])
    ]


def _split_ref(raw: str) -> tuple[str, int | None]:
    """(path, highest cited line or None), after stripping a `::node` and a
    trailing `:N` / `:N-M`."""
    path = raw.rstrip(".:").split("::")[0]
    m = _LINE_SUFFIX.match(path)
    if m:
        return m.group(1), int(m.group(3) or m.group(2))
    return path, None


def _resolves(root: Path, path: str) -> bool:
    if path.endswith("/**"):
        # Path.glob on 3.11 returns only directories for a trailing `**`.
        return (root / path[:-3]).is_dir()
    if "*" in path:
        return bool(glob.glob(str(root / path)))
    return (root / path).exists()


def _repo_refs(text: str) -> list[tuple[str, str, int | None]]:
    """(raw token, expanded path, cited line) for every backticked repo path."""
    refs = []
    for span in _spans(text):
        for m in _PATH_TOKEN.finditer(span):
            path, line = _split_ref(m.group(1))
            for expanded in _expand_braces(path):
                refs.append((m.group(1), expanded, line))
    return refs


def _root_line_refs(text: str, root: Path) -> list[tuple[str, str, int]]:
    """`Dockerfile:41`-style citations of an existing top-level file."""
    refs = []
    for span in _spans(text):
        for m in _ROOT_FILE_LINE.finditer(span):
            name = m.group(1)
            if (root / name).is_file():
                refs.append((m.group(0), name, int(m.group(3) or m.group(2))))
    return refs


def _missing(text: str, root: Path) -> list[str]:
    return sorted(
        {raw for raw, path, _ in _repo_refs(text) if path not in ALLOWLIST and not _resolves(root, path)}
    )


def _out_of_range(text: str, root: Path) -> list[str]:
    bad = []
    refs = [(raw, path, line) for raw, path, line in _repo_refs(text) if line is not None]
    refs += _root_line_refs(text, root)
    for raw, path, line in refs:
        target = root / path
        if target.is_file():
            length = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            if line > length:
                bad.append(f"{raw} (file has {length} lines)")
    return sorted(set(bad))


def test_deploy_box_migrations_exist():
    slugs = [s for text in _texts().values() for s in _DEPLOY_BOX.findall(text)]
    assert len(slugs) > 10, f"control: expected the deploy boxes, parsed only {slugs}"
    versions = ROOT / "alembic" / "versions"
    missing = [s for s in slugs if not (versions / f"{s}.py").is_file()]
    assert not missing, f"runbook deploy boxes name migrations that do not exist: {missing}"


def test_referenced_repo_paths_exist():
    texts = _texts()
    assert sum(len(_repo_refs(t)) for t in texts.values()) > 50, (
        "control: the path scanner found almost nothing"
    )
    missing = [f"{rel}: {raw}" for rel, text in texts.items() for raw in _missing(text, ROOT)]
    assert not missing, (
        f"the runbook names repo paths that do not exist: {missing}. Fix the reference, "
        "or add the path to ALLOWLIST with the reason it is named on purpose."
    )


def test_line_references_are_in_range():
    bad = [f"{rel}: {ref}" for rel, text in _texts().items() for ref in _out_of_range(text, ROOT)]
    assert not bad, f"the runbook cites lines past the end of the file: {bad}"


def test_allowlist_entries_are_needed():
    named = {path for text in _texts().values() for _, path, _ in _repo_refs(text)}
    stale = [p for p in ALLOWLIST if p not in named or _resolves(ROOT, p)]
    assert not stale, f"ALLOWLIST entries the runbook no longer needs: {stale}"
    assert all(reason.strip() for reason in ALLOWLIST.values()), "every ALLOWLIST entry needs a reason"


def test_claude_md_files_stay_under_the_recommended_length():
    long = {
        path.relative_to(ROOT).as_posix(): n
        for path in (CLAUDE_MD, *NESTED)
        if (n := len(path.read_text(encoding="utf-8").splitlines())) >= MAX_CLAUDE_MD_LINES
    }
    assert not long, (
        f"CLAUDE.md files at or over {MAX_CLAUDE_MD_LINES} lines: {long}. Move reference "
        "detail to docs/operations/ and keep a pointer."
    )


def test_every_claude_md_is_listed():
    """A tracked nested CLAUDE.md outside NESTED would escape every check above, and
    one under prompts/ would be read as a prompt file. Untracked files are ignored, so
    a git-ignored scratch directory cannot fail this on one machine only."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", ":(glob)**/CLAUDE.md"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    found = {ROOT / rel for rel in listed if rel and rel != "CLAUDE.md"}
    assert found == set(NESTED), (
        f"tracked nested CLAUDE.md files {sorted(str(p) for p in found)} != NESTED "
        f"{sorted(str(p) for p in NESTED)}"
    )


def test_claude_md_indexes_every_operations_page():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    pages = sorted(p.relative_to(ROOT).as_posix() for p in OPERATIONS.glob("*.md"))
    assert len(pages) >= 5, f"control: expected the operations pages, found {pages}"
    unindexed = [page for page in pages if f"`{page}`" not in text]
    assert not unindexed, f"CLAUDE.md's index does not name {unindexed}"


def test_the_scanner_flags_planted_bad_references():
    """Control: a planted missing path and an out-of-range line are caught, and
    the forms the real file uses (braces, globs, `/**`, `::node`, ranges) pass."""
    good = (
        "`src/main.py:1` `src/main.py:1-2` `docs/specs/2026-08-07-{pi,hub}-bot-prompts.md` "
        "`prompts/roles/*/role.toml` `prompts/roles/**` "
        "`tests/unit/test_claude_md_references.py::test_x` `Dockerfile:1`\n"
        "```bash\necho `src/fenced_is_ignored.py`\n```\n"
    )
    assert _missing(good, ROOT) == []
    assert _out_of_range(good, ROOT) == []

    assert _missing("see `src/nope.py` and `docs/specs/{a,b}.md`", ROOT) == [
        "docs/specs/{a,b}.md",
        "src/nope.py",
    ]
    assert _missing("a wrapped `docker compose run\n> python scripts/nope.sh`", ROOT) == [
        "scripts/nope.sh"
    ]
    assert _missing("run `./scripts/nope.sh`", ROOT) == ["scripts/nope.sh"]
    assert _missing("run `./scripts/ci.sh`", ROOT) == []
    [flagged] = _out_of_range("`src/main.py:999999`", ROOT)
    assert flagged.startswith("src/main.py:999999 ")
    [flagged] = _out_of_range("`Dockerfile:1-999999`", ROOT)
    assert flagged.startswith("Dockerfile:1-999999 ")
