"""No operator-facing text names the other deployment's containers or project.

A second, unrelated CoPI deployment (org1) shares the production host. Its
simulation container carries the UNPREFIXED name this repo's container extends,
and its compose project name prefixes its networks and images, so a command
copied from this repo that names either acts on org1's production stack.

The allowlisted files name them only inside explicit "never touch this"
warnings.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FOREIGN = re.compile(r"(?<![\w-])agent-run(?![\w-])|copi-python[-_]")

# Files whose mentions are explicit warnings, not instructions.
ALLOWLIST = {
    "scripts/provision_slack_bots.py",
    "scripts/migrate/preflight.py",
    "scripts/migrate/run_migration.sh",
}


def _scanned_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in ("templates/**/*.html", "src/**/*.py", "specs/**/*.md",
                    "scripts/**/*.py", "scripts/**/*.sh"):
        files.update(ROOT.glob(pattern))
    files.add(ROOT / "docker-compose.yml")
    return sorted(f for f in files if f.is_file())


def test_the_pattern_separates_the_two_deployments():
    assert FOREIGN.search("docker stop agent-run")
    assert FOREIGN.search("--network copi-python_default")
    assert FOREIGN.search("copi-python-app")
    assert not FOREIGN.search("docker stop -t 420 blackbird-agent-run")
    assert not FOREIGN.search("copi-blackbird-agent-1")


def test_the_scan_covers_the_expected_trees():
    rels = {f.relative_to(ROOT).as_posix() for f in _scanned_files()}
    assert "templates/admin/_cohort_gate_banner.html" in rels
    assert "specs/cohort-system-v2.md" in rels
    assert "scripts/export_agent_roster.py" in rels
    assert "docker-compose.yml" in rels
    assert any(r.startswith("src/") for r in rels)


def test_no_foreign_deployment_names_outside_warnings():
    hits = []
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FOREIGN.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    assert not hits, "org1 names outside an allowlisted warning:\n" + "\n".join(hits)
