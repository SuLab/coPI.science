"""build_info: image-baked JSON preferred, pure-Python .git parse as fallback.

The agent image has no git binary but does carry .git (Dockerfile `COPY . .`,
no .dockerignore), so the runtime reader must never shell out to git.
"""
import json

from src.services.build_info import BuildInfo, get_build_info


def _git_dir(tmp_path, head: str, refs: dict[str, str] | None = None,
             packed: str | None = None):
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text(head, encoding="utf-8")
    for ref, sha in (refs or {}).items():
        p = g / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sha + "\n", encoding="utf-8")
    if packed is not None:
        (g / "packed-refs").write_text(packed, encoding="utf-8")


def test_prefers_build_info_json(tmp_path):
    (tmp_path / ".build_info.json").write_text(json.dumps(
        {"commit": "a" * 40, "branch": "blackbird", "dirty_files": 1}
    ), encoding="utf-8")
    _git_dir(tmp_path, "ref: refs/heads/other\n")  # must be ignored

    info = get_build_info(tmp_path)

    assert info == BuildInfo("a" * 40, "blackbird", 1, "build_info_json")


def test_falls_back_to_git_dir_loose_ref(tmp_path):
    _git_dir(tmp_path, "ref: refs/heads/blackbird\n",
             refs={"refs/heads/blackbird": "b" * 40})

    info = get_build_info(tmp_path)

    assert info.commit == "b" * 40
    assert info.branch == "blackbird"
    assert info.dirty_files is None  # unknowable without git
    assert info.source == "git_dir"


def test_git_dir_packed_ref(tmp_path):
    packed = (
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{'c' * 40} refs/heads/blackbird\n"
        f"{'d' * 40} refs/tags/v1\n"
        f"^{'e' * 40}\n"
    )
    _git_dir(tmp_path, "ref: refs/heads/blackbird\n", packed=packed)

    info = get_build_info(tmp_path)

    assert info.commit == "c" * 40
    assert info.branch == "blackbird"


def test_branch_names_with_slashes_survive(tmp_path):
    _git_dir(tmp_path, "ref: refs/heads/feat/x\n",
             refs={"refs/heads/feat/x": "f" * 40})

    assert get_build_info(tmp_path).branch == "feat/x"


def test_detached_head(tmp_path):
    _git_dir(tmp_path, "1" * 40 + "\n")

    info = get_build_info(tmp_path)

    assert info.commit == "1" * 40
    assert info.branch is None


def test_nothing_available(tmp_path):
    assert get_build_info(tmp_path) == BuildInfo(None, None, None, "unavailable")


def test_malformed_json_falls_through_to_git_dir(tmp_path):
    (tmp_path / ".build_info.json").write_text("{not json", encoding="utf-8")
    _git_dir(tmp_path, "ref: refs/heads/blackbird\n",
             refs={"refs/heads/blackbird": "9" * 40})

    info = get_build_info(tmp_path)

    assert info.source == "git_dir"
    assert info.commit == "9" * 40
