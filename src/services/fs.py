"""Filesystem helpers shared by the web app and the engine."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a concurrent reader sees the old file or
    the new one, never an empty or torn one (spec §8.4 PS-18): write a temp
    file in the same directory, flush and fsync it, then ``os.replace`` it over
    ``path``. The caller creates the directory. On failure the temp file is
    removed and the exception propagates."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
