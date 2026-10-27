"""BlackbirdBot projects' own prompt documents, ``prompts/projects/`` (owner, 2026-10-26: the
interview prompt sets stay frozen and the projects prototype uses separate documents).

No file here is in a role's ``prompt_files`` (``role_capabilities.py``), so editing one never
moves a ``prompt_set_stamp``, a ``role.toml`` version or the run-start announcement. The
worker reads ``research.md`` and ``choose.md`` itself (``src/services/project_research.py``);
the four files below are the engine's. ``Agent`` reads them through the start-time
``PromptSnapshot``, so a mid-run edit cannot change the next turn and shows as ``projects``
drift on /workspace/simulation instead.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from src.agent import roles as _roles

#: BlackbirdBot's system prompt in a project thread (it carries no rubric).
HUB_SYSTEM_FILE = "hub-system.md"
#: BlackbirdBot's reply template in a project thread.
HUB_THREAD_REPLY_FILE = "hub-thread-reply.md"
#: A lab's top-level reply to BlackbirdBot's posting.
LAB_POSTING_REPLY_FILE = "lab-posting-reply.md"
#: A lab's reply template in a project thread.
LAB_THREAD_REPLY_FILE = "lab-thread-reply.md"
#: The engine's project prompts, in hash order.
ENGINE_PROMPT_FILES: tuple[str, ...] = (
    HUB_SYSTEM_FILE, HUB_THREAD_REPLY_FILE, LAB_POSTING_REPLY_FILE, LAB_THREAD_REPLY_FILE,
)


def prompt_path(filename: str) -> Path:
    """``prompts/projects/<filename>``, from ``roles.PROMPTS_DIR`` at call time."""
    return _roles.PROMPTS_DIR / "projects" / filename


def read(filename: str) -> str | None:
    """The file's text, or None when it is missing."""
    try:
        return prompt_path(filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def content_hash() -> str:
    """12 hex digits over ``ENGINE_PROMPT_FILES`` keyed by filename, a missing file hashing
    as ``b"<missing>"``: ``roles.prompt_set_stamp``'s scheme."""
    h = hashlib.sha256()
    for name in ENGINE_PROMPT_FILES:
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(prompt_path(name).read_bytes())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()[:12]
