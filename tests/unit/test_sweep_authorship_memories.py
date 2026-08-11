"""The one-time memory sweep for issue #29."""

from pathlib import Path

from scripts.sweep_authorship_memories import sweep
from src.agent.authorship_rules import LabPublicationRecord
from tests.unit.test_authorship_rules import POISONED_MEMORY_ROW


def _write_memory(root: Path, agent_id: str, text: str) -> Path:
    p = root / agent_id / "public.md"
    p.parent.mkdir(parents=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_dry_run_reports_but_does_not_write(tmp_path):
    p = _write_memory(tmp_path, "good", f"{POISONED_MEMORY_ROW}\n- Keep me.\n")
    findings = sweep(tmp_path, records={}, fix=False)
    assert findings == [("good", [POISONED_MEMORY_ROW])]
    assert "Desiderata" in p.read_text()  # untouched


def test_fix_writes_cleaned_file_with_backup(tmp_path):
    p = _write_memory(tmp_path, "good", f"{POISONED_MEMORY_ROW}\n- Keep me.\n")
    findings = sweep(tmp_path, records={}, fix=True)
    assert findings == [("good", [POISONED_MEMORY_ROW])]
    assert "Desiderata" not in p.read_text()
    assert "Keep me." in p.read_text()
    backup = p.with_suffix(".md.pre-sweep")
    assert "Desiderata" in backup.read_text()


def test_grounded_memory_untouched(tmp_path):
    rec = LabPublicationRecord(dois={"10.1093/bioadv/vbag036"}, has_records=True)
    text = "We co-authored Desiderata (https://doi.org/10.1093/bioadv/vbag036).\n"
    p = _write_memory(tmp_path, "wu", text)
    findings = sweep(tmp_path, records={"wu": rec}, fix=True)
    assert findings == []
    assert p.read_text() == text


def test_legacy_flat_layout_is_swept(tmp_path):
    # Agents not yet migrated to the partitioned layout keep their working
    # memory at profiles/memory/<agent_id>.md (no subdirectory) — see
    # Agent.public_working_memory's fallback. sweep() must see this file too.
    p = tmp_path / "good.md"
    p.write_text(f"{POISONED_MEMORY_ROW}\n- Keep me.\n", encoding="utf-8")

    findings = sweep(tmp_path, records={}, fix=False)
    assert findings == [("good", [POISONED_MEMORY_ROW])]
    assert "Desiderata" in p.read_text()  # untouched by dry run

    findings = sweep(tmp_path, records={}, fix=True)
    assert findings == [("good", [POISONED_MEMORY_ROW])]
    assert "Desiderata" not in p.read_text()
    assert "Keep me." in p.read_text()
    backup = p.with_suffix(".md.pre-sweep")
    assert "Desiderata" in backup.read_text()


def test_unreadable_file_does_not_abort_the_sweep(tmp_path, monkeypatch, capsys):
    # One agent's file is fine; another's raises on read (permission error,
    # bad encoding, whatever). The error must be reported, not raised — and
    # must not swallow the finding already gathered from the good file.
    good = _write_memory(tmp_path, "good", f"{POISONED_MEMORY_ROW}\n- Keep me.\n")
    broken = tmp_path / "broken.md"
    broken.write_text("placeholder\n", encoding="utf-8")

    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == broken:
            raise OSError("simulated permission error")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    findings = sweep(tmp_path, records={}, fix=False)

    assert findings == [("good", [POISONED_MEMORY_ROW])]
    assert "Desiderata" in good.read_text()  # dry run: untouched either way
    err = capsys.readouterr().out
    assert "broken" in err
    assert "ERROR" in err
