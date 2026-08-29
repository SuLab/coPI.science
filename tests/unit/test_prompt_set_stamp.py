"""prompt_set_stamp: the declared version + content hash of a role's prompt set.

Mirrors the rubric's pattern (blackbird_rubric.py: [meta].version +
sha256[:12] of the bytes): the version is a human declaration, the hash is the
drift alarm that catches an edit nobody bumped the version for.
"""
import pytest

from src.agent import roles
from src.agent.roles import ROLE_PROMPT_FILES, load_role, prompt_set_stamp


def _prompt_tree(tmp_path, monkeypatch):
    """A minimal prompts/ tree covering both roles' file sets."""
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    for name in ROLE_PROMPT_FILES["pi_lab"]:
        (tmp_path / name).write_text(f"base {name}", encoding="utf-8")
    hub = tmp_path / "roles" / "scout_hub"
    hub.mkdir(parents=True)
    (hub / "role.toml").write_text('version = "2.0.0"\nlabel = "Hub"\n', encoding="utf-8")
    for name in ROLE_PROMPT_FILES["scout_hub"]:
        (hub / name).write_text(f"hub {name}", encoding="utf-8")
    pi = tmp_path / "roles" / "pi_lab"
    pi.mkdir(parents=True)
    (pi / "role.toml").write_text('version = "1.5.0"\n', encoding="utf-8")
    return tmp_path


def test_version_comes_from_role_toml(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    assert prompt_set_stamp("scout_hub").version == "2.0.0"
    assert prompt_set_stamp("pi_lab").version == "1.5.0"


def test_missing_version_key_reads_unversioned(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    hub_manifest = tmp_path / "roles" / "scout_hub" / "role.toml"
    hub_manifest.write_text('label = "Hub"\n', encoding="utf-8")
    assert prompt_set_stamp("scout_hub").version == "unversioned"


def test_hash_is_12_hex_and_stable(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    a = prompt_set_stamp("scout_hub").content_hash
    b = prompt_set_stamp("scout_hub").content_hash
    assert a == b
    assert len(a) == 12
    int(a, 16)  # raises if not hex


def test_hash_changes_when_a_prompt_file_changes(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    before = prompt_set_stamp("scout_hub").content_hash
    hub_file = tmp_path / "roles" / "scout_hub" / "agent-system.md"
    hub_file.write_text("hub agent-system.md EDITED", encoding="utf-8")
    assert prompt_set_stamp("scout_hub").content_hash != before


def test_pi_hash_ignores_hub_overrides_and_vice_versa(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    pi_before = prompt_set_stamp("pi_lab").content_hash
    hub_file = tmp_path / "roles" / "scout_hub" / "identity.md"
    hub_file.write_text("hub identity EDITED", encoding="utf-8")
    assert prompt_set_stamp("pi_lab").content_hash == pi_before


def test_pi_hash_covers_phase5_but_hub_hash_does_not(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    hub_before = prompt_set_stamp("scout_hub").content_hash
    pi_before = prompt_set_stamp("pi_lab").content_hash
    (tmp_path / "phase5-new-post.md").write_text("EDITED", encoding="utf-8")
    assert prompt_set_stamp("scout_hub").content_hash == hub_before
    assert prompt_set_stamp("pi_lab").content_hash != pi_before


def test_unknown_role_raises_key_error(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    with pytest.raises(KeyError):
        prompt_set_stamp("grantbot")


def test_missing_file_hashes_as_missing_not_crash(tmp_path, monkeypatch):
    _prompt_tree(tmp_path, monkeypatch)
    (tmp_path / "phase5-new-post.md").unlink()
    stamp = prompt_set_stamp("pi_lab")  # must not raise
    assert len(stamp.content_hash) == 12


def test_real_role_tomls_declare_a_version_and_still_parse():
    """Against the REAL prompts/ tree: the version keys this task adds must
    exist, and load_role must keep ignoring them (it reads only label/tools/
    calls_per_load_per_window/post_types)."""
    for role in ("scout_hub", "pi_lab"):
        assert prompt_set_stamp(role).version not in ("", "unversioned")
    spec = load_role("scout_hub")
    assert spec.label == "Scout Hub"  # unchanged by the new key
