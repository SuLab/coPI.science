"""Rewrite the three prompt-set specs from the prompt files and code strings they embed.

`docs/specs/2026-08-07-{pi,hub}-bot-prompts.md` reproduce every prompt file
verbatim inside *Source:*-labelled four-backtick blocks, and their section 4
reproduces the `thread_guidance` strings. Nothing generated them: they were
maintained by hand, so any prompt edit silently drifted them and
`tests/unit/test_doc_prompt_sync.py` failed on the NEXT full run rather than at
the edit. This is that test's inverse -- it deliberately mirrors the test's own
regexes, so the two cannot disagree about what a block is.

`docs/specs/2026-10-27-projects-prompts.md` does the same for BlackbirdBot projects
(`prompts/projects/`); its ````text blocks are code strings (`project_code_blocks`).

Run after any edit to `prompts/**`, to `_PI_LAB`/`_SCOUT_HUB`, or to the projects'
`RESEARCH_ANGLES`, `FINAL_ANSWER_TEXT` or `src/agent/project_guidance.py`:

    .venv-test/bin/python scripts/sync_prompt_set_docs.py [--check]

`--check` reports drift and exits 1 without writing, for use in a hook.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.project_guidance import project_turn  # noqa: E402
from src.agent.thread_guidance import phase4_guidance  # noqa: E402
from src.services.project_research import (  # noqa: E402
    FINAL_ANSWER_TEXT,
    RESEARCH_ANGLES,
    RESEARCH_DOMAINS,
)

ROOT = Path(__file__).resolve().parents[1]
DOCS = {
    "pi_lab": ROOT / "docs/specs/2026-08-07-pi-bot-prompts.md",
    "scout_hub": ROOT / "docs/specs/2026-08-07-hub-bot-prompts.md",
    "projects": ROOT / "docs/specs/2026-10-27-projects-prompts.md",
}
# Mirrors _BLOCK_RE in tests/unit/test_doc_prompt_sync.py, but captures the
# block body as its own group so it can be replaced in place.
_BLOCK_RE = re.compile(
    r"(\*Source: `([^`]+)`[^*]*\*.*?\n````(?:markdown|text)?\n)(.*?)(\n````)",
    re.DOTALL,
)
_PHASE_COUNTS = {"EXPLORE": 2, "DECIDE": 5, "MUST CONCLUDE": 12}


def _sync_embedded_files(text: str) -> tuple[str, list[str]]:
    """Replace every `.md`-sourced block with that file's current contents."""
    changed: list[str] = []

    def repl(m: "re.Match[str]") -> str:
        head, src, body, tail = m.group(1), m.group(2), m.group(3), m.group(4)
        if not src.endswith(".md"):
            return m.group(0)
        disk = (ROOT / src).read_text(encoding="utf-8").rstrip("\n")
        if disk != body.rstrip("\n"):
            changed.append(src)
        return f"{head}{disk}{tail}"

    return _BLOCK_RE.sub(repl, text), changed


def _sync_section_4(text: str, role: str) -> tuple[str, bool]:
    """Replace section 4's six ````text blocks with the live guidance strings."""
    marker = "## 4. Interview phase guidance"
    if marker not in text:
        return text, False
    before, rest = text.split(marker, 1)
    section, after = rest.split("\n## 5.", 1)

    wanted: list[str] = []
    for _phase, count in _PHASE_COUNTS.items():
        _, guidance, instructions = phase4_guidance(role, count)
        wanted += [guidance, instructions]

    blocks = re.findall(r"````text\n(.*?)\n````", section, re.DOTALL)
    if len(blocks) != 6:
        raise SystemExit(
            f"{role}: section 4 has {len(blocks)} ````text blocks, expected 6 -- "
            "the document's shape changed and this script must be revisited"
        )

    # Judged EXACTLY, not whitespace-normalised. `test_doc_prompt_sync.py`
    # compares section 4 with whitespace collapsed, so a block that differs only
    # in wrapping passes the test -- but the rewrite below is byte-exact, so a
    # normalised comparison here would report "in sync" and then still rewrite
    # the file. That made `--check` exit 0 on a document a plain run would
    # change, which is the one thing a --check mode must never do.
    drifted = [i for i, b in enumerate(blocks) if b != wanted[i]]
    it = iter(wanted)
    section = re.sub(
        r"(````text\n)(?:.*?)(\n````)",
        lambda m: f"{m.group(1)}{next(it)}{m.group(2)}",
        section,
        flags=re.DOTALL,
    )
    return f"{before}{marker}{section}\n## 5.{after}", bool(drifted)


def project_code_blocks() -> list[str]:
    """The projects document's ````text blocks, in document order: the five research angles
    (``RESEARCH_DOMAINS`` order), ``FINAL_ANSWER_TEXT``, BlackbirdBot's OPEN, REFINE and
    MUST CLOSE guidance and instructions, the paragraph its guidance gains when the lab
    agrees, then the lab's OPEN, REFINE and FINAL WORD guidance and instructions.
    ``tests/unit/test_doc_prompt_sync.py`` mirrors this list."""
    blocks = [RESEARCH_ANGLES[domain] for domain in RESEARCH_DOMAINS]
    blocks.append(FINAL_ANSWER_TEXT)
    for ordinal in (2, 6, 12):
        turn = project_turn("scout_hub", ordinal, lab_agrees=False)
        blocks += [turn.guidance, turn.instructions]
    base = project_turn("scout_hub", 2, lab_agrees=False).guidance + "\n\n"
    agreeing = project_turn("scout_hub", 2, lab_agrees=True).guidance
    if not agreeing.startswith(base):
        raise SystemExit("projects: the early-close paragraph is no longer appended to the "
                         "OPEN guidance -- this script must be revisited")
    blocks.append(agreeing[len(base):])
    for ordinal in (2, 6, 10):
        turn = project_turn("pi_lab", ordinal, lab_agrees=False)
        blocks += [turn.guidance, turn.instructions]
    return blocks


def _sync_code_blocks(text: str, doc_name: str) -> tuple[str, bool]:
    """Replace the projects document's ````text blocks, in order, with
    ``project_code_blocks()``. Judged exactly, as section 4 is."""
    wanted = project_code_blocks()
    blocks = re.findall(r"````text\n(.*?)\n````", text, re.DOTALL)
    if len(blocks) != len(wanted):
        raise SystemExit(
            f"{doc_name}: {len(blocks)} ````text blocks, expected {len(wanted)} -- "
            "the document's shape changed and this script must be revisited"
        )
    it = iter(wanted)
    text = re.sub(
        r"(````text\n)(?:.*?)(\n````)",
        lambda m: f"{m.group(1)}{next(it)}{m.group(2)}",
        text,
        flags=re.DOTALL,
    )
    return text, blocks != wanted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = ap.parse_args()

    drift = False
    for role, doc in DOCS.items():
        original = doc.read_text(encoding="utf-8")
        text, changed = _sync_embedded_files(original)
        text, sec4 = _sync_section_4(text, role)
        code = False
        if role == "projects":
            text, code = _sync_code_blocks(text, doc.name)
        if changed or sec4 or code:
            drift = True
            for src in changed:
                print(f"{doc.name}: {src} drifted")
            if sec4:
                print(f"{doc.name}: section 4 ({role} guidance) drifted")
            if code:
                print(f"{doc.name}: code-sourced blocks drifted")
        if not args.check and text != original:
            doc.write_text(text, encoding="utf-8")
            print(f"{doc.name}: rewritten")

    if args.check and drift:
        print("drift found; run without --check to rewrite", file=sys.stderr)
        return 1
    if not drift:
        print("all blocks already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
