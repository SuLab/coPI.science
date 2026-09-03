"""Static Dockerfile structure checks for #27 I3 (layer order, multi-stage,
non-root — Tasks 27.6/27.7/27.8). No `docker build` here: these assert the
*text* is ordered/shaped correctly. Real image-build verification is a manual
step noted in each task's Deploy note (mirrors nginx's `nginx -t` check in
Task 27.12)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile").read_text()


def test_dependencies_install_from_the_lockfile_before_source_is_copied():
    text = _dockerfile()
    assert "requirements.lock" in text, "Dockerfile must install from the hash-pinned lockfile"
    assert "--require-hashes" in text
    lock_copy = text.index("requirements.lock")
    pip_install_lock = text.index("--require-hashes")
    src_copy = text.index("COPY src/ src/")
    assert lock_copy < pip_install_lock < src_copy, (
        "deps must install from requirements.lock BEFORE src/ is copied, so a "
        "source-only change doesn't bust the dependency layer"
    )
