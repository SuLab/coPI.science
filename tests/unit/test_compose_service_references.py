"""Operator text never sends a compose command to ``app``.

Scanned: templates, the root and nested CLAUDE.md files, and docs/operations/. A
command written as ``$DC …`` is not recognised (the grammar needs a literal
``docker compose``), so most of the CLAUDE.md files' commands are out of reach.

``app`` is the web service of ``docker-compose.yml``, the dev stack (``--reload``, the
whole repo bind-mounted, host ``:8001``); production's web service is
``blackbird-app``. An instruction that ``exec``s, ``run``s or ``up``s ``app`` either
fails with ``service "app" is not defined`` against the prod file or, bare, acts on the
dev stack. A line that names ``docker-compose.yml`` is explicitly about the dev stack
and is exempt. ``scripts/`` is covered by ``test_operator_commands.py``.

There is deliberately no test of ``docker-compose.prod.yml`` itself: the committed
file still names the web service ``app`` and the rename lives only in the host's
uncommitted working tree (D5 in docs/plans/2026-09-25-rca-remediation-plan.md), so a
test of either version fails somewhere. RCA §8 cause 5 is recorded as open instead,
in docs/audits/open-findings.md.
"""

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SUBCOMMANDS = "exec|run|up|build|logs|stop|restart"
# Compose's global options; see test_operator_commands.py, which uses the same grammar.
_VALUE_OPTS = (
    r"(?:-f|--file|-p|--project-name|--profile|--env-file|--project-directory"
    r"|--ansi|--progress|--parallel)"
)
_FLAG_OPTS = r"(?:--dry-run|--compatibility|--all-resources)"
_GLOBAL_OPT = rf"(?:{_VALUE_OPTS}(?:=|\s+)\S+|{_FLAG_OPTS})"

# ``rest`` is the command's own text: it stops at a closing backtick, an HTML tag, a
# shell comment or a shell operator, so prose after the command is not read as
# its arguments.
COMPOSE_COMMAND = re.compile(
    rf"\bdocker[ -]compose\s+(?:{_GLOBAL_OPT}\s+)*(?P<sub>{_SUBCOMMANDS})(?![\w-])"
    r"(?P<rest>[^`<#;|&\n]*)"
)
# The dev service as a delimited token. ``app\b`` would flag ``blackbird-app`` (a word
# boundary sits between ``-`` and ``a``), the ``/app/...`` container paths and
# ``src.main:app``.
DEV_APP = re.compile(r"(?<![\w/.:-])app(?![\w/:-])")
DEV_FILE = "docker-compose.yml"


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(first physical line number, line)``, joining backslash continuations.

    A continuation inside a markdown quote block starts with ``>``, which is dropped
    along with the indentation.
    """
    start = 0
    parts: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not parts:
            start = lineno
        body = raw.rstrip()
        continued = body.endswith("\\")
        body = body.rstrip("\\")
        parts.append(body.lstrip(" \t>").strip() if parts else body.rstrip())
        if continued:
            continue
        yield start, " ".join(parts)
        parts = []
    if parts:
        yield start, " ".join(parts)


def dev_app_commands(line: str) -> list[str]:
    """Return each compose command on ``line`` whose target is the dev ``app``."""
    if DEV_FILE in line:
        return []
    return [
        m.group(0).strip()
        for m in COMPOSE_COMMAND.finditer(line)
        if DEV_APP.search(m.group("rest"))
    ]


def _scanned_files(root: Path) -> list[Path]:
    files = set(root.glob("templates/**/*.html"))
    files.add(root / "CLAUDE.md")
    for directory in ("src", "alembic"):
        files.update((root / directory).rglob("CLAUDE.md"))
    files.update(root.glob("docs/operations/*.md"))
    return sorted(f for f in files if f.is_file())


def scan(root: Path) -> tuple[list[str], int]:
    """Return ``path:line: command`` failures and the number of commands recognised."""
    failures: list[str] = []
    commands = 0
    for path in _scanned_files(root):
        rel = path.relative_to(root).as_posix()
        for lineno, line in _logical_lines(path.read_text(encoding="utf-8", errors="replace")):
            commands += len(COMPOSE_COMMAND.findall(line))
            for command in dev_app_commands(line):
                failures.append(f"{rel}:{lineno}: {command}")
    return failures, commands


FLAGGED = [
    "docker compose exec app python x",
    "docker compose exec -T app python x",
    "docker compose -f docker-compose.prod.yml exec -T app python x",
    "<code>docker compose run --rm app alembic upgrade head</code>",
    "> `docker compose up -d app`",
]

LEGAL = [
    "docker compose -f docker-compose.prod.yml exec -T blackbird-app python x",
    'docker compose -f docker-compose.prod.yml run --rm -v "$PWD/data:/app/data" blackbird-app python x',
    "uvicorn src.main:app",
    "docker compose -f docker-compose.prod.yml run --rm blackbird-app uvicorn src.main:app",
    "blackbird-app:/app/scripts/",
    "docker compose -f docker-compose.prod.yml cp scripts/x.py blackbird-app:/app/scripts/",
    "docker compose -f docker-compose.yml exec app python x",
    "> Bare `docker compose` resolves to `docker-compose.yml`, whose web service is named `app`",
    "the committed file still names the web service `app`",
    "`docker compose run agent` then starts the previous image, not the app",
    "<code>docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate blackbird-app</code>",
]


def test_flagged_forms_are_flagged():
    missed = [line for line in FLAGGED if not dev_app_commands(line)]
    assert not missed, "should have been flagged:\n" + "\n".join(missed)


def test_legal_forms_pass():
    wrong = {line: dev_app_commands(line) for line in LEGAL if dev_app_commands(line)}
    assert not wrong, f"flagged a legal line: {wrong}"


def test_continuation_lines_are_joined():
    text = ">     docker compose -f docker-compose.prod.yml exec -T \\\n>       app python x\n"
    lines = list(_logical_lines(text))
    assert len(lines) == 1 and lines[0][0] == 1
    assert dev_app_commands(lines[0][1])


def test_the_scanner_flags_a_planted_dev_service_command(tmp_path):
    (tmp_path / "templates" / "admin").mkdir(parents=True)
    (tmp_path / "templates" / "admin" / "x.html").write_text(
        "<p>run <code>docker compose exec -T app python x</code></p>\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "prose\n\n    docker compose -f docker-compose.prod.yml exec -T blackbird-app python x\n",
        encoding="utf-8",
    )
    failures, commands = scan(tmp_path)
    assert commands == 2
    assert failures == ["templates/admin/x.html:1: docker compose exec -T app python x"]


def test_no_operator_text_targets_the_dev_web_service():
    failures, commands = scan(ROOT)
    # Anti-vacuity: docs/operations/ and the cohort banner hold several compose
    # commands; the CLAUDE.md files mostly write `$DC …`, which is not counted.
    assert commands >= 5, f"only {commands} compose commands recognised; the grammar has drifted"
    assert not failures, "compose instructions aimed at the dev `app` service:\n" + "\n".join(failures)
