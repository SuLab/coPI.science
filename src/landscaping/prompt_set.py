"""The landscaping prompt set, its stamp, and the BlackbirdBot extracts (spec §8.1-§8.2).

``prompts/landscaping/set.toml`` records the set ``version`` and, under ``[files]``, the
hex sha256 of every other file in the directory, ``criteria.toml`` included. A file whose
bytes do not match its record, a recorded file that is missing, or a file with no record
is an unrecorded edit: ``load_prompt_set`` refuses it. ``stamp`` writes the record.

The BlackbirdBot extracts are cut verbatim, by heading, from the frozen scout_hub prompt
files at load (D16); a missing heading raises ``PromptSetError`` (fail-fast, like the
rubric loader). Those files are only ever read.

CLI: ``python -m src.landscaping.prompt_set --stamp <version>``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.landscaping.criteria import Criteria
from src.services.blackbird_rubric import render_rubric_markdown
from src.services.llm import SYNTHESIS_DATA_NOTICE

PROMPT_DIR = Path("prompts/landscaping")
SET_FILE = "set.toml"
CHAT_FILE = "chat.md"

# The prompt files that are epoch inputs. chat.md judges nothing (D61).
JUDGING_FILES = ("screen.md", "extract.md", "triage.md", "deep.md", "rerank.md", "similarity.md",
                 "cluster_label.md", "final.md")

# Stage name (src/landscaping/llm_client.py STAGES, plus the chat) -> prompt file. S6's
# full-text re-extraction reuses the extraction prompt.
_STAGE_FILES = {
    "screen": "screen.md",
    "extract": "extract.md",
    "triage": "triage.md",
    "deep": "deep.md",
    "deep_extract": "extract.md",
    "rerank": "rerank.md",
    "similarity": "similarity.md",
    "label": "cluster_label.md",
    "final": "final.md",
    "chat": CHAT_FILE,
}

# Extracts appended after each stage's filled prompt, in this order (spec §8.2 table).
_STAGE_EXTRACTS = {
    "triage": ("standards", "practice"),
    "deep": ("standards", "practice"),
    "final": ("standards", "practice", "contract"),
}

_SCOUT_HUB_DIR = Path("prompts/roles/scout_hub")
_STANDARDS_HEADING = "## Opportunity Assessment Quality Standards"
_RUBRIC_LINE = "{rubric}"
_PRACTICE_STARTS = ("**What this looks like in practice.**", "Or: an institution has chemical matter")
_CONTRACT_HEADING = "### Concluding with an Opportunity Assessment: the sidecar"


class PromptSetError(RuntimeError):
    """The prompt set or a BlackbirdBot extract cannot be loaded as recorded."""


@dataclass(frozen=True)
class PromptSet:
    """The loaded set. ``files`` maps every file name but ``set.toml`` to its text;
    ``judging_hashes`` maps each ``JUDGING_FILES`` entry to its hex sha256.
    ``content_hash`` is the hex sha256 of the ``"{name} {hash}"`` lines for the sorted
    file names, joined by newlines."""

    version: str
    content_hash: str
    files: dict[str, str]
    judging_hashes: dict[str, str]


@dataclass(frozen=True)
class BlackbirdExtracts:
    """Verbatim scout_hub text (spec §8.2). ``standards`` ends with the rendered rubric in
    place of the ``{rubric}`` line. ``scout_hub_version`` is the scout_hub ``role.toml``
    version; ``content_hash`` is the hex sha256 of the three extracts."""

    standards: str
    practice: str
    contract: str
    scout_hub_version: str
    content_hash: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_record(dir: Path) -> tuple[str, dict[str, str]]:
    """``(version, {file name: hash})`` from ``set.toml``; raises ``PromptSetError``."""
    path = dir / SET_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PromptSetError(f"prompt set record unreadable at {path}: {exc}") from exc
    version = data.get("version")
    files = data.get("files", {})
    if not isinstance(version, str) or not version:
        raise PromptSetError(f"{path}: `version` must be a non-empty string")
    if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
        raise PromptSetError(f"{path}: [files] must map file names to hash strings")
    return version, files


def _current_hashes(dir: Path) -> dict[str, str]:
    """The hash of every regular file in ``dir`` but ``set.toml``."""
    try:
        paths = [p for p in dir.iterdir() if p.is_file() and p.name != SET_FILE]
        return {p.name: _sha256(p.read_bytes()) for p in paths}
    except OSError as exc:
        raise PromptSetError(f"prompt set directory unreadable at {dir}: {exc}") from exc


def _content_hash(hashes: dict[str, str]) -> str:
    lines = "\n".join(f"{name} {hashes[name]}" for name in sorted(hashes))
    return _sha256(lines.encode("utf-8"))


def load_prompt_set(dir: Path = PROMPT_DIR) -> PromptSet:
    """Load the set, refusing any unrecorded edit; raises ``PromptSetError``."""
    version, recorded = _read_record(dir)
    current = _current_hashes(dir)
    unrecorded = sorted(set(current) - set(recorded))
    missing = sorted(set(recorded) - set(current))
    changed = sorted(n for n in set(current) & set(recorded) if current[n] != recorded[n])
    if unrecorded or missing or changed:
        raise PromptSetError(
            f"prompt set {dir} does not match {SET_FILE} (unrecorded edit): "
            f"unrecorded={unrecorded} missing={missing} changed={changed}; "
            "re-stamp with a new version"
        )
    absent = [n for n in JUDGING_FILES if n not in current]
    if absent:
        raise PromptSetError(f"prompt set {dir} lacks judging files {absent}")
    try:
        files = {n: (dir / n).read_text(encoding="utf-8") for n in sorted(current)}
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptSetError(f"prompt set file unreadable in {dir}: {exc}") from exc
    return PromptSet(
        version=version,
        content_hash=_content_hash(current),
        files=files,
        judging_hashes={n: current[n] for n in JUDGING_FILES},
    )


def chat_prompt_matches(dir: Path = PROMPT_DIR) -> bool:
    """Whether ``chat.md`` matches its recorded hash. False on any read or record
    failure, so the chat refuses rather than answers on an unrecorded edit (spec §8.1)."""
    try:
        _, recorded = _read_record(dir)
        actual = _sha256((dir / CHAT_FILE).read_bytes())
    except (PromptSetError, OSError):
        return False
    return recorded.get(CHAT_FILE) == actual


def stamp(version: str, dir: Path = PROMPT_DIR) -> None:
    """Record ``version`` and every file's hash in ``set.toml``, rewriting it whole.

    Refuses (``PromptSetError``) when any file changed, appeared or disappeared since
    the record and ``version`` equals the recorded version: an edit needs a new
    version (spec §8.1). A missing ``set.toml`` counts as no record.
    """
    if not version.strip():
        raise PromptSetError("stamp: version must be a non-empty string")
    current = _current_hashes(dir)
    if (dir / SET_FILE).exists():
        old_version, recorded = _read_record(dir)
        if current != recorded and version == old_version:
            raise PromptSetError(
                f"stamp: files changed since version {old_version}; stamp a new version"
            )
    lines = [
        "# Written by `python -m src.landscaping.prompt_set --stamp <version>`; do not edit",
        "# by hand. [files] holds the sha256 of every other file in this directory.",
        f"version = {json.dumps(version)}",
        "",
        "[files]",
        *(f"{json.dumps(name)} = {json.dumps(current[name])}" for name in sorted(current)),
    ]
    (dir / SET_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _line_start(text: str, line: str, source: Path, after: int = 0) -> int:
    """Offset of the first line equal to ``line`` at or after ``after``."""
    match = re.compile(rf"^{re.escape(line)}[ \t]*$", re.M).search(text, after)
    if match is None:
        raise PromptSetError(f"{source}: expected heading or line not found: {line!r}")
    return match.start()


def _paragraph(text: str, start: str, source: Path) -> str:
    """The paragraph (up to the next blank line) whose first line begins with ``start``."""
    match = re.compile(rf"^{re.escape(start)}", re.M).search(text)
    if match is None:
        raise PromptSetError(f"{source}: expected paragraph not found: {start!r}")
    end = text.find("\n\n", match.start())
    return text[match.start():end if end != -1 else len(text)].rstrip()


def _standards(text: str, source: Path) -> str:
    start = _line_start(text, _STANDARDS_HEADING, source)
    rubric_at = _line_start(text, _RUBRIC_LINE, source, after=start)
    return text[start:rubric_at] + render_rubric_markdown()


def _contract(text: str, source: Path) -> str:
    """From the sidecar heading to the next heading of level 1-3, or the end of file
    (the post-skeleton rules close the file today)."""
    start = _line_start(text, _CONTRACT_HEADING, source)
    nxt = re.compile(r"^#{1,3} ", re.M).search(text, start + len(_CONTRACT_HEADING))
    return text[start:nxt.start() if nxt else len(text)].rstrip()


def load_blackbird_extracts() -> BlackbirdExtracts:
    """Cut the §8.2 extracts from the frozen scout_hub files; raises ``PromptSetError``
    when a file is unreadable or an expected heading or paragraph start is missing."""
    agent_path = _SCOUT_HUB_DIR / "agent-system.md"
    phase4_path = _SCOUT_HUB_DIR / "phase4-thread-reply.md"
    role_path = _SCOUT_HUB_DIR / "role.toml"
    try:
        agent_text = agent_path.read_text(encoding="utf-8")
        phase4_text = phase4_path.read_text(encoding="utf-8")
        role = tomllib.loads(role_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PromptSetError(f"scout_hub prompt files unreadable: {exc}") from exc
    version = role.get("version")
    if not isinstance(version, str) or not version:
        raise PromptSetError(f"{role_path}: `version` must be a non-empty string")
    standards = _standards(agent_text, agent_path)
    practice = "\n\n".join(_paragraph(agent_text, s, agent_path) for s in _PRACTICE_STARTS)
    contract = _contract(phase4_text, phase4_path)
    joined = "\n\n".join((standards, practice, contract))
    return BlackbirdExtracts(
        standards=standards,
        practice=practice,
        contract=contract,
        scout_hub_version=version,
        content_hash=_sha256(joined.encode("utf-8")),
    )


def _criteria_vocab(criteria: Criteria) -> str:
    return "\n".join((
        f"- Directions: {', '.join(criteria.directions)}",
        f"- Stages: {', '.join(criteria.stages)}",
        f"- Evidence grades, weakest to strongest: {' < '.join(criteria.evidence_grades)}",
        f"- Agent kinds: {', '.join(criteria.agent_kinds)}",
    ))


def _deal_breakers(criteria: Criteria) -> str:
    label = {"data": "data test", "llm": "your judgement"}
    return "\n".join(
        f"- `{d.key}` ({label.get(d.test, d.test)}): {d.description}" for d in criteria.deal_breakers
    )


def _target_rubric(rubric) -> str:
    """The rubric's evidence items, numbered 1..n across dimensions in file order
    (spec §7.4 step 2): today scientific_credibility's 5, then translational_path's 6."""
    lines: list[str] = []
    n = 0
    for dim in rubric.dimensions:
        if not dim.evidence:
            continue
        lines.append(f"{dim.title} (`{dim.key}`):")
        for item in dim.evidence:
            n += 1
            lines.append(f"{n}. {item}")
    return "\n".join(lines)


def system_prompt(stage: str, *, ps: PromptSet, ex: BlackbirdExtracts, criteria: Criteria, rubric) -> str:
    """The system prompt for ``stage``: its file with the five placeholders filled, then
    the stage's BlackbirdBot extracts (spec §8.2). ``rubric`` is a parsed rubric
    (``load_rubric()``). The fenced dossier belongs in the user message, built by the
    stage runner. Raises ``PromptSetError`` for an unknown stage or an absent file."""
    name = _STAGE_FILES.get(stage)
    if name is None or name not in ps.files:
        raise PromptSetError(f"no prompt file for stage {stage!r}")
    # str.replace, not str.format: the prompts carry literal JSON braces.
    text = ps.files[name]
    for placeholder, value in (
        ("{criteria_scope}", criteria.scope_text),
        ("{criteria_vocab}", _criteria_vocab(criteria)),
        ("{deal_breakers}", _deal_breakers(criteria)),
        ("{target_rubric}", _target_rubric(rubric)),
        ("{data_notice}", SYNTHESIS_DATA_NOTICE),
    ):
        text = text.replace(placeholder, value)
    extracts = [getattr(ex, field) for field in _STAGE_EXTRACTS.get(stage, ())]
    return "\n\n".join([text.rstrip(), *extracts])


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.landscaping.prompt_set")
    parser.add_argument("--stamp", metavar="VERSION", required=True,
                        help="record VERSION and every file's hash in set.toml")
    args = parser.parse_args(argv)
    try:
        stamp(args.stamp)
        ps = load_prompt_set()
    except PromptSetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"stamped {PROMPT_DIR / SET_FILE}: version {ps.version}, content hash {ps.content_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
