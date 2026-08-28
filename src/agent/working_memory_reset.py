"""Archive-and-reset of ``profiles/memory/*`` for ``--fresh`` runs.

Working memory is an LLM-synthesized, cross-run verdict ledger injected into
every agent system prompt (``Agent._compose_working_memory``). A ``--fresh``
run exists to be a clean experiment, so it must not start with the previous
runs' screening history in its prompts — but the files are also the only
record of what the agents "learned", so they are MOVED, never deleted.

Plain (non ``--fresh``) resumes never call this: memory continuity across a
restart of the SAME run is the point of the files.
"""
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVE_DIR_NAME = "archive"


def archive_working_memory(memory_dir: Path, *, now: float | None = None) -> Path | None:
    """Move every entry of ``memory_dir`` (except the archive itself) into
    ``memory_dir/archive/<UTC stamp>/``.

    Returns the archive directory, or ``None`` when ``memory_dir`` does not
    exist or holds nothing but prior archives. Same-filesystem ``Path.rename``
    moves — the memory tree and its archive live under one bind mount.
    """
    if not memory_dir.is_dir():
        return None
    entries = [p for p in memory_dir.iterdir() if p.name != ARCHIVE_DIR_NAME]
    if not entries:
        return None

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    dest = memory_dir / ARCHIVE_DIR_NAME / stamp
    n = 1
    while dest.exists():
        dest = memory_dir / ARCHIVE_DIR_NAME / f"{stamp}-{n}"
        n += 1
    dest.mkdir(parents=True)

    for path in entries:
        path.rename(dest / path.name)
    logger.info(
        "Archived working memory for %d agent(s) to %s", len(entries), dest
    )
    return dest
