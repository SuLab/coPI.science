"""Static Dockerfile structure checks: layer order, multi-stage build, and
non-root user. No `docker build` here: these assert the *text* is
ordered/shaped correctly. Real image-build verification is a manual step,
mirroring nginx's `nginx -t` check."""

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
    lines = text.splitlines()
    from_line_indices = [i for i, line in enumerate(lines) if line.startswith("FROM ")]
    assert len(from_line_indices) == 2, "expected exactly a builder stage and a runtime stage"
    assert "FROM python:3.11-slim" in text, (
        "the --no-build-isolation step depends on the base image shipping "
        "setuptools/wheel"
    )
    assert "AS builder" in text
    assert "--from=builder" in text
    runtime_section = "\n".join(lines[from_line_indices[-1] :])
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
    for target in ("profiles", "data", "logs"):
        assert target in chown_lines[-1], f"{target} must be chowned to the runtime user"
    assert "static" not in chown_lines[-1], (
        "static/ is served read-only by StaticFiles and nothing in src/ writes "
        "to it — chowning it to the runtime user is a needless stored-XSS "
        "surface on browser-served assets"
    )
    mkdir_lines = [line for line in text.splitlines() if "mkdir -p" in line]
    assert mkdir_lines, "expected an mkdir -p line provisioning the writable dirs"
    assert "static" not in mkdir_lines[-1], "static/ must not be (re-)created/owned by the mkdir step either"


def test_bytecode_is_compiled_before_dropping_root():
    text = _dockerfile()
    assert "compileall" in text, "bake the bytecode cache while root still owns src/"
    assert "python -m compileall" in text
    copy_idx = text.rindex("COPY . .")
    compileall_idx = text.index("compileall")
    user_idx = text.rindex("USER 10001")
    assert copy_idx < compileall_idx, "compileall must run after the app tree is copied in"
    assert compileall_idx < user_idx, (
        "compileall must run BEFORE USER drops root — UID 10001 cannot write "
        "__pycache__ under root-owned src/"
    )


def test_home_env_is_set_for_the_runtime_user():
    text = _dockerfile()
    assert "ENV HOME=/app" in text, "no-create-home leaves $HOME unset; the runtime user needs one"
