"""Atomic text writes: tempfile-in-same-dir + os.replace.

`Path.write_text` truncates before writing, so this PROCESS crashing or being
OOM-killed mid-write leaves a truncated file on disk — the next reader (an
agent's prompt loader, the profile exporter) gets a corrupt, partial document
with no error. `os.replace` on the same filesystem is atomic: readers see
either the old complete file or the new complete file, never a partial one.
This guarantees no torn file if the PROCESS dies mid-write; it does not add an
`fsync`, so it does not guarantee durability across a machine crash/power loss
(out of scope — V6-24a is about truncation, not power-loss durability).
See issue #22 COR-24.
"""

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically.

    Writes to a temp file in the SAME directory as ``path`` (so the final
    ``os.replace`` is a same-filesystem rename, not a cross-device copy) then
    replaces ``path`` in one syscall. The caller is responsible for creating
    ``path.parent`` first (mirrors the existing ``mkdir(parents=True,
    exist_ok=True)`` call at every site this replaces).
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        # mkstemp ALWAYS creates 0600 regardless of umask, so without this the
        # replace silently tightens every file it rewrites (measured: 0664 -> 0600).
        # Keep the mode the file already had; 0644 for a new one, which is what
        # Path.write_text produced under this project's umask.
        try:
            os.chmod(tmp_name, stat.S_IMODE(os.stat(path).st_mode))
        except FileNotFoundError:
            os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
