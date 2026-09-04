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


def test_two_stage_build_with_a_slim_runtime():
    text = _dockerfile()
    from_lines = [line for line in text.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2, "expected exactly a builder stage and a runtime stage"
    assert "AS builder" in text
    assert "--from=builder" in text
    runtime_section = text[text.rindex("FROM "):]
    assert "gcc" not in runtime_section
    assert "libpq-dev" not in runtime_section
    assert "libpq5" in runtime_section


def test_runtime_stage_drops_to_a_non_root_fixed_uid():
    text = _dockerfile()
    assert "USER 10001" in text
    assert "useradd" in text and "10001" in text
    assert "--uid 10001" in text
    assert "--gid 10001" in text
    user_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("USER ")]
    assert user_lines, "no USER directive found"
    assert user_lines[-1] == "USER 10001", "the LAST USER directive must be USER 10001"
    user_idx = text.rindex("USER 10001")
    copy_idx = text.rindex("COPY . .")
    assert copy_idx < user_idx, "USER must be set after the app tree is copied in"


def test_local_package_installs_without_deps_or_build_isolation():
    text = _dockerfile()
    assert "--no-deps" in text
    assert "--no-build-isolation" in text, (
        "without --no-build-isolation, `pip install .` fetches setuptools/wheel "
        "from PyPI unhashed at build time even for a --no-deps install"
    )


def test_ownership_is_scoped_to_writable_dirs_not_the_whole_app_tree():
    text = _dockerfile()
    assert "chown -R 10001:10001 /app" not in text, (
        "src/, templates/, alembic/ and scripts/ must stay root-owned and "
        "read-only to the runtime user, not swept up by a whole-tree chown"
    )
    chown_lines = [line for line in text.splitlines() if "chown -R 10001:10001" in line]
    assert chown_lines, "expected a chown line granting the runtime user its writable dirs"
    for target in ("profiles", "data", "logs", "static"):
        assert target in chown_lines[-1], f"{target} must be chowned to the runtime user"


def test_home_env_is_set_for_the_runtime_user():
    text = _dockerfile()
    assert "ENV HOME=/app" in text, "no-create-home leaves $HOME unset; the runtime user needs one"
