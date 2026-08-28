"""--fresh must reset working memory (archive, never delete): the files are a
cross-run verdict ledger injected into every system prompt (audit F1)."""
from pathlib import Path

from src.agent.working_memory_reset import archive_working_memory


def _seed_memory(memory: Path) -> None:
    (memory / "blackbird").mkdir(parents=True)
    (memory / "blackbird" / "public.md").write_text("verdict ledger\n")
    (memory / "blackbird" / "private").mkdir()
    (memory / "blackbird" / "private" / "C123.md").write_text("private notes\n")
    (memory / "agre.md").write_text("legacy flat file\n")


def test_archive_moves_agent_dirs_and_legacy_files(tmp_path):
    memory = tmp_path / "memory"
    _seed_memory(memory)

    dest = archive_working_memory(memory, now=1787900000.0)

    assert dest is not None and dest.parent == memory / "archive"
    assert not (memory / "blackbird").exists()
    assert not (memory / "agre.md").exists()
    assert (dest / "blackbird" / "public.md").read_text() == "verdict ledger\n"
    assert (dest / "blackbird" / "private" / "C123.md").exists()
    assert (dest / "agre.md").exists()


def test_archive_returns_none_when_there_is_nothing_to_move(tmp_path):
    assert archive_working_memory(tmp_path / "absent") is None
    empty = tmp_path / "memory"
    empty.mkdir()
    assert archive_working_memory(empty) is None


def test_archive_never_touches_prior_archives_and_never_collides(tmp_path):
    memory = tmp_path / "memory"
    _seed_memory(memory)
    first = archive_working_memory(memory, now=1787900000.0)
    _seed_memory(memory)
    second = archive_working_memory(memory, now=1787900000.0)

    assert first != second, "same-second fresh starts must not collide"
    assert (first / "blackbird" / "public.md").exists(), "prior archive disturbed"
    assert (second / "blackbird" / "public.md").exists()
    # Only 'archive' remains in the live tree.
    assert [p.name for p in memory.iterdir()] == ["archive"]
