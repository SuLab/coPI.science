"""Prompt file loader with {{include: filename}} support."""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path("prompts")


def load_prompt(path: str | Path, default: str = "") -> str:
    """Load a prompt file, resolving {{include: filename}} directives.

    Include paths are resolved relative to the prompts/ directory.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default

    def _resolve(match: re.Match) -> str:
        included_path = PROMPTS_DIR / match.group(1).strip()
        try:
            return included_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"[include not found: {included_path}]"

    return re.sub(r"\{\{include:\s*(.+?)\}\}", _resolve, text)
