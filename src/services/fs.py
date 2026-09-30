"""Filesystem helpers shared by the web app and the engine."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _current_umask() -> int:
    """The process umask, read without changing it where the platform allows
    (``/proc/self/status``); otherwise set-and-restore, which is momentary."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a concurrent reader sees the old file or
    the new one, never an empty or torn one (spec §8.4 PS-18): write a temp
    file in the same directory, flush and fsync it, then ``os.replace`` it over
    ``path``. The caller creates the directory. On failure the temp file is
    removed and the exception propagates.

    The result has the permissions a plain ``open(path, "w")`` would leave:
    an existing file keeps its mode, and a new one gets ``0o666 & ~umask``.
    ``mkstemp`` alone creates 0600, which ``os.replace`` would carry over."""
    path = Path(path)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_current_umask()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), mode)
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
