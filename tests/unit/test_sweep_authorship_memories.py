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


def test_sweep_strips_own_lab_third_person_claim_with_identity(tmp_path):
    # Audit finding I5: "Good Lab co-authored ..." in GOOD's own memory is a
    # self-claim wearing a third-person subject. The sweep must pass each
    # agent's identity so the exemption doesn't shield it.
    from src.agent.authorship_rules import lab_self_names

    line = "Good Lab co-authored the Desiderata paper."
    p = _write_memory(tmp_path, "good", f"{line}\n- Keep me.\n")
    identities = {"good": lab_self_names("good", "GoodBot", "Benjamin Good")}

    findings = sweep(tmp_path, records={}, fix=True, identities=identities)
    assert findings == [("good", [line])]
    assert "Desiderata" not in p.read_text()
    assert "Keep me." in p.read_text()


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


def test_profile_grounded_memory_survives_fix(tmp_path):
    # Audit finding I6: _load_records was DB-only while the runtime guard is
    # DB ∪ profile — so --fix deleted TRUE memory grounded only in the
    # agent's profile DOIs. The sweep must union profile DOIs exactly like
    # _reject_ungrounded_authorship does.
    from scripts.sweep_authorship_memories import _augment_with_profile_dois

    profiles = tmp_path / "profiles"
    (profiles / "public").mkdir(parents=True)
    (profiles / "public" / "wu.md").write_text(
        "Representative publication: BioThings Explorer — "
        "https://doi.org/10.1093/bioinformatics/btad570\n",
        encoding="utf-8",
    )
    memory_root = tmp_path / "memory"
    line = (
        "We published BioThings Explorer "
        "(https://doi.org/10.1093/bioinformatics/btad570) - cite in intros."
    )
    p = _write_memory(memory_root, "wu", f"{line}\n")

    records = _augment_with_profile_dois({}, profiles)  # zero DB rows
    findings = sweep(memory_root, records, fix=True)

    assert findings == []
    assert p.read_text() == f"{line}\n"


def test_private_profile_dois_also_count(tmp_path):
    # Mirrors Agent.own_publication_dois: public AND private profiles.
    from scripts.sweep_authorship_memories import _augment_with_profile_dois

    profiles = tmp_path / "profiles"
    (profiles / "private").mkdir(parents=True)
    (profiles / "private" / "wu.md").write_text(
        "Own paper: 10.1093/bioadv/vbag036\n", encoding="utf-8"
    )
    records = _augment_with_profile_dois({}, profiles)
    assert records["wu"].has_records is True
    assert "10.1093/bioadv/vbag036" in records["wu"].dois


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
