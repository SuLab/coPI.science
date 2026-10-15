"""ProfileRevision rows are built in exactly one place, so every writer stamps the owner
(spec 2026-10-15 §10 R5.18; C6)."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_profile_revision_has_one_constructor():
    out = subprocess.run(
        ["git", "grep", "-n", "-F", "ProfileRevision(", "--", "src", "scripts"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    files = sorted({line.split(":", 1)[0] for line in out.splitlines()})
    assert files == ["src/models/profile_revision.py", "src/services/profile_versioning.py"]
    assert "class ProfileRevision(Base):" in out
