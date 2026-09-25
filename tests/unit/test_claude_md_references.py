"""CLAUDE.md's repo references must resolve (RCA §8 cause 4: prose that nothing reads).

CLAUDE.md is the operator runbook, and outside the two sync-tested anchors nothing
read it: deploy boxes, file paths and `path:N` citations drifted silently as the
tree moved underneath them. This module checks the mechanically checkable part:

- every "Deploy order for `00NN_slug`" box names a migration that exists;
- every backticked path under one of `REPO_PREFIXES` exists (brace groups
  expanded, `*` treated as a glob, `/**` as "this directory exists");
- every `path:N` / `path:N-M` citation is within the cited file's length.

It cannot tell whether a cited line still says what the prose claims; it only
catches references that point at nothing.
"""
import glob
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = ROOT / "CLAUDE.md"

REPO_PREFIXES = ("src", "scripts", "tests", "templates", "prompts", "alembic", "docs", "specs")

# Repo paths CLAUDE.md names on purpose although they do not exist in a clean
# checkout, each with the reason. Keep it empty unless a path is deliberately
# untracked or removed; a stale entry fails `test_allowlist_entries_are_needed`.
ALLOWLIST: dict[str, str] = {}

_PATH_TOKEN = re.compile(
    r"(?<![\w./~$-])((?:" + "|".join(REPO_PREFIXES) + r")/"
    r"(?:[^\s`'\"(),;<>{}]|\{[^{}\s`]*\})*)"
)
# A top-level file cited with a line number, e.g. `Dockerfile:41`.
_ROOT_FILE_LINE = re.compile(r"(?<![\w./~$-])([A-Za-z][\w.-]*):(\d+)(?:-(\d+))?\b")
_LINE_SUFFIX = re.compile(r"^(.*?):(\d+)(?:-(\d+))?$")
_DEPLOY_BOX = re.compile(r"Deploy order for `(\d{4}_\w+)`")


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
    slugs = _DEPLOY_BOX.findall(CLAUDE_MD.read_text(encoding="utf-8"))
    assert len(slugs) > 10, f"control: expected the deploy boxes, parsed only {slugs}"
    versions = ROOT / "alembic" / "versions"
    missing = [s for s in slugs if not (versions / f"{s}.py").is_file()]
    assert not missing, f"CLAUDE.md deploy boxes name migrations that do not exist: {missing}"


def test_referenced_repo_paths_exist():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert len(_repo_refs(text)) > 50, "control: the path scanner found almost nothing"
    missing = _missing(text, ROOT)
    assert not missing, (
        f"CLAUDE.md names repo paths that do not exist: {missing}. Fix the reference, "
        "or add the path to ALLOWLIST with the reason it is named on purpose."
    )


def test_line_references_are_in_range():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    bad = _out_of_range(text, ROOT)
    assert not bad, f"CLAUDE.md cites lines past the end of the file: {bad}"


def test_allowlist_entries_are_needed():
    text = CLAUDE_MD.read_text(encoding="utf-8")
    named = {path for _, path, _ in _repo_refs(text)}
    stale = [p for p in ALLOWLIST if p not in named or _resolves(ROOT, p)]
    assert not stale, f"ALLOWLIST entries CLAUDE.md no longer needs: {stale}"
    assert all(reason.strip() for reason in ALLOWLIST.values()), "every ALLOWLIST entry needs a reason"


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
    [flagged] = _out_of_range("`src/main.py:999999`", ROOT)
    assert flagged.startswith("src/main.py:999999 ")
    [flagged] = _out_of_range("`Dockerfile:1-999999`", ROOT)
    assert flagged.startswith("Dockerfile:1-999999 ")
