"""Unit tests for src/services/atomic_write.py (issue #22 COR-24)."""

import stat
import tempfile

import pytest

from src.services import atomic_write as aw
from src.services.atomic_write import atomic_write_text


def test_writes_the_given_text(tmp_path):
    p = tmp_path / "out.md"
    atomic_write_text(p, "hello\n")
    assert p.read_text(encoding="utf-8") == "hello\n"


def test_overwrite_is_all_or_nothing_not_truncate_then_write(tmp_path, monkeypatch):
    """The defect this replaces: Path.write_text truncates the target before
    writing the new content, so a crash mid-write leaves a corrupt, partial
    file. atomic_write_text must never leave the target in a truncated state —
    simulate a write failure and confirm the ORIGINAL file is untouched."""
    p = tmp_path / "out.md"
    p.write_text("original content\n", encoding="utf-8")

    def _boom(*a, **kw):
        raise OSError("disk full (simulated)")

    # Patches the process-global `os` module for the duration of this test (not
    # just `aw`'s reference to it) — accepted rather than `monkeypatch.setattr(aw,
    # "os", SimpleNamespace(...))` because nothing else runs in this window.
    monkeypatch.setattr(aw.os, "fdopen", _boom)

    with pytest.raises(OSError):
        atomic_write_text(p, "new content\n")

    assert p.read_text(encoding="utf-8") == "original content\n"  # untouched, not truncated


def test_no_leftover_temp_file_on_failure(tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(aw.os, "fdopen", _boom)

    with pytest.raises(OSError):
        atomic_write_text(tmp_path / "out.md", "x")

    assert list(tmp_path.iterdir()) == []


def test_writes_into_the_same_directory_as_the_target(tmp_path, monkeypatch):
    """os.replace requires the temp file to be on the same filesystem as the
    target — a temp dir elsewhere (e.g. the system tmpdir) would make the
    final rename a cross-device copy, defeating atomicity."""
    calls = []
    real_mkstemp = tempfile.mkstemp

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(aw.tempfile, "mkstemp", _spy)
    atomic_write_text(tmp_path / "out.md", "x")
    assert calls == [tmp_path]


def test_the_targets_permissions_survive_the_replace(tmp_path):
    """mkstemp creates 0600; os.replace would carry that onto the target and
    quietly tighten every profile file the system rewrites."""
    p = tmp_path / "out.md"
    p.write_text("original\n", encoding="utf-8")
    p.chmod(0o664)
    atomic_write_text(p, "new\n")
    assert stat.S_IMODE(p.stat().st_mode) == 0o664
