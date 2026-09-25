"""Operator commands in ``scripts/`` target this repo's production stack (RCA §8.4).

Two compose stacks share the production host. A bare ``docker compose`` resolves to
``docker-compose.yml``, the dev stack, whose web service is ``app``; the deployed stack
is ``docker-compose.prod.yml``, whose web service is ``blackbird-app``. The unprefixed
``agent-run`` container is org1's production simulation, and this repo's own CLI run
container, ``blackbird-agent-run``, exists only on the emergency path now that the
supervisor owns normal operation. A command pasted from a script docstring or printed
by a script runs as written, so every ``scripts/**/*.{py,sh}`` line is held to these
rules:

* a ``docker compose`` COMMAND (the words followed, after global options only, by a
  subcommand) names ``-f docker-compose.prod.yml`` before the subcommand;
* nothing ``exec``s into, or copies to, the dev service ``app``;
* ``blackbird-agent-run`` is stopped, inspected or named only on a line that says
  "emergency";
* no ``&& docker compose`` chain: a failed first step must not be hidden in one line an
  operator pastes whole.

The rules match commands, not prose: "a bare ``docker compose`` resolves to the dev
stack" names no subcommand, and "the container is ``blackbird-agent-run``" names no
docker verb, so both stay legal. Lines continued with a trailing backslash are joined
first, so a ``-f`` on a continuation line still counts. ``$DC``/``"${DC[@]}"`` forms are
not scanned: the literal ``docker compose`` appears only where the variable is defined.
"""

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SUBCOMMANDS = "exec|run|up|build|stop|start|restart|cp|logs|ps|down|rm|pull|kill"
# Compose's global options. Only these may sit between ``docker compose`` and the
# subcommand; anything else means the words are prose, not a command.
_VALUE_OPTS = (
    r"(?:-f|--file|-p|--project-name|--profile|--env-file|--project-directory"
    r"|--ansi|--progress|--parallel)"
)
_FLAG_OPTS = r"(?:--dry-run|--compatibility|--all-resources)"
_GLOBAL_OPT = rf"(?:{_VALUE_OPTS}(?:=|\s+)\S+|{_FLAG_OPTS})"

# ``docker-compose`` (v1) is matched too; ``docker-compose.yml`` is not, since a
# filename is followed by ``.`` rather than whitespace.
COMPOSE_COMMAND = re.compile(
    rf"\bdocker[ -]compose\s+(?P<opts>(?:{_GLOBAL_OPT}\s+)*)(?P<sub>{_SUBCOMMANDS})(?![\w-])"
)
PROD_FILE = re.compile(r"(?:-f|--file)(?:=|\s+)[\"']?(?:\./)?docker-compose\.prod\.yml[\"']?(?!\S)")

# The dev service as a delimited token. ``\bapp`` would also match ``blackbird-app``
# (``\b`` sits between ``-`` and ``a``) and the ``/app/...`` container paths.
EXEC_DEV_APP = re.compile(r"\bexec(?: -T)?(?: -e \S+)* (?<![\w/.:-])app(?![\w/:-])")
COPY_DEV_APP = re.compile(r"(?<![\w-])app:/app")

AGENT_RUN_CONTAINER = re.compile(
    r"\bdocker (?:stop|rm|start|restart|kill|logs|inspect|exec)\b[^\n]*\bblackbird-agent-run\b"
    r"|--name blackbird-agent-run\b"
)
AND_CHAIN = re.compile(r"&&\s*docker[ -]compose\b")

# (path, stripped line) -> reason. Exempt from every rule.
EXEMPT: dict[tuple[str, str], str] = {
    ("scripts/migrate/run_migration.sh", 'DC=(docker compose -f "$COMPOSE_FILE")'): (
        "the script's one compose invocation; COMPOSE_FILE defaults to "
        "docker-compose.prod.yml and is an operator override"
    ),
}


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(first physical line number, line)``, joining backslash continuations.

    A Python docstring writes the shell continuation as ``\\\\``, so every trailing
    backslash is stripped before joining.
    """
    start = 0
    parts: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not parts:
            start = lineno
        body = raw.rstrip()
        continued = body.endswith("\\")
        parts.append(body.rstrip("\\").strip() if parts else body.rstrip("\\").rstrip())
        if continued:
            continue
        yield start, " ".join(parts)
        parts = []
    if parts:
        yield start, " ".join(parts)


def violations(line: str) -> list[str]:
    """Return the rules ``line`` breaks, by name (empty when it is legal)."""
    found = []
    for m in COMPOSE_COMMAND.finditer(line):
        if not PROD_FILE.search(m.group("opts")):
            found.append(f"`docker compose {m.group('sub')}` without -f docker-compose.prod.yml")
    if EXEC_DEV_APP.search(line):
        found.append("exec into the dev service `app`")
    if COPY_DEV_APP.search(line):
        found.append("copy to the dev service `app`")
    if AGENT_RUN_CONTAINER.search(line) and "emergency" not in line.lower():
        found.append("blackbird-agent-run outside an emergency-path line")
    if AND_CHAIN.search(line):
        found.append("`&& docker compose` chain")
    return found


def scan(root: Path) -> tuple[list[str], int]:
    """Scan ``root/scripts/**/*.{py,sh}``.

    Returns ``path:line: rule: text`` failures and the number of compose commands
    recognised, which the real-tree test uses as its anti-vacuity control.
    """
    files: set[Path] = set()
    for pattern in ("scripts/**/*.py", "scripts/**/*.sh"):
        files.update(root.glob(pattern))
    failures: list[str] = []
    commands = 0
    for path in sorted(f for f in files if f.is_file()):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in _logical_lines(text):
            if (rel, line.strip()) in EXEMPT:
                continue
            commands += len(COMPOSE_COMMAND.findall(line))
            for rule in violations(line):
                failures.append(f"{rel}:{lineno}: {rule}: {line.strip()}")
    return failures, commands


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

FLAGGED = [
    "docker compose exec app python x",
    "docker compose exec -T app python x",
    "docker compose -f docker-compose.prod.yml exec -T app python x",
    "docker compose -f docker-compose.prod.yml exec -T -e A=1 app python x",
    "docker compose -f docker-compose.prod.yml cp scripts/x.py app:/app/scripts/",
    "docker compose --profile agent run --rm agent python -m src.agent.main",
    "docker compose run --rm blackbird-app python x",
    "docker compose -f docker-compose.yml up -d",
    "docker-compose exec blackbird-app python x",
    '    "  docker compose exec -T postgres pg_dump -U copi -d copi | gzip "',
    "cd /srv && docker compose -f docker-compose.prod.yml up -d blackbird-app",
    "docker stop -t 420 blackbird-agent-run",
    "docker logs blackbird-agent-run > run.log",
    "docker compose -f docker-compose.prod.yml --profile agent run -d --name blackbird-agent-run agent",
]

LEGAL = [
    "docker compose -f docker-compose.prod.yml exec -T blackbird-app python x",
    'docker compose -f docker-compose.prod.yml run --rm -v "$PWD/data:/app/data" blackbird-app python x',
    "uvicorn src.main:app",
    "docker compose -f docker-compose.prod.yml cp scripts/x.py blackbird-app:/app/scripts/",
    "blackbird-app:/app/scripts/",
    "docker compose -f docker-compose.prod.yml --profile agent up -d --no-deps --force-recreate agent",
    "docker compose --profile agent -f docker-compose.prod.yml stop -t 420 agent",
    "docker compose --file=docker-compose.prod.yml ps",
    'DC="docker compose -f docker-compose.prod.yml"',
    "# a bare `docker compose` resolves to the dev stack, whose web service is `app`",
    "# Bare docker compose resolves to docker-compose.yml.",
    "docker stop -t 420 blackbird-agent-run   # ONLY if an emergency CLI run is live",
    "# the CLI run's container is blackbird-agent-run, never the unprefixed one",
    '  if ! "${DC[@]}" exec -T "$PG_SVC" pg_dump -U copi -Fc -f "$CTMP" "$DBNAME"; then',
    "docker compose -f docker-compose.prod.yml build blackbird-app worker",
]


def test_flagged_forms_are_flagged():
    missed = [line for line in FLAGGED if not violations(line)]
    assert not missed, "should have been flagged:\n" + "\n".join(missed)


def test_legal_forms_pass():
    wrong = {line: violations(line) for line in LEGAL if violations(line)}
    assert not wrong, f"flagged a legal line: {wrong}"


def test_continuation_lines_are_joined():
    ok = "    docker compose -f docker-compose.prod.yml run --rm --no-deps -T \\\\\n        blackbird-app python x\n"
    assert [violations(line) for _, line in _logical_lines(ok)] == [[]]
    late_f = "docker compose \\\n  -f docker-compose.prod.yml exec -T blackbird-app x\n"
    assert [violations(line) for _, line in _logical_lines(late_f)] == [[]]
    bare = "docker compose \\\n  exec -T \\\n  app python x\nnext line\n"
    lines = list(_logical_lines(bare))
    assert [n for n, _ in lines] == [1, 4]
    assert violations(lines[0][1])


def test_the_exemption_is_not_stale():
    for (rel, line), _reason in EXEMPT.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert line in (raw.strip() for raw in text.splitlines()), f"stale exemption: {rel}: {line}"


def test_the_scanner_flags_a_planted_bare_command(tmp_path):
    planted = tmp_path / "scripts" / "sub" / "planted.sh"
    planted.parent.mkdir(parents=True)
    planted.write_text("#!/bin/bash\necho ok\ndocker compose exec -T app python x\n", encoding="utf-8")
    (tmp_path / "scripts" / "fine.py").write_text(
        '"""docker compose -f docker-compose.prod.yml exec -T blackbird-app python x"""\n',
        encoding="utf-8",
    )
    failures, commands = scan(tmp_path)
    assert commands == 2
    assert len(failures) == 2  # the missing -f and the dev service, on one line
    assert all(f.startswith("scripts/sub/planted.sh:3: ") for f in failures)


def test_scripts_issue_only_prod_stack_commands():
    failures, commands = scan(ROOT)
    # Anti-vacuity: the scripts' docstrings and printed hints hold dozens of commands.
    assert commands >= 20, f"only {commands} compose commands recognised; the grammar has drifted"
    assert not failures, "operator commands that miss the prod stack:\n" + "\n".join(failures)
