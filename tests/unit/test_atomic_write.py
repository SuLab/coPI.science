import os
import threading

from src.services.fs import atomic_write_text


def test_a_reader_never_sees_an_empty_or_partial_file(tmp_path):
    path = tmp_path / "p.md"
    atomic_write_text(path, "x" * 100_000)
    stop = threading.Event()
    bad = []

    def _reader():
        while not stop.is_set():
            data = path.read_text(encoding="utf-8")
            if len(data) != 100_000:
                bad.append(len(data))

    t = threading.Thread(target=_reader)
    t.start()
    for ch in "abcdefghij":
        atomic_write_text(path, ch * 100_000)
    stop.set()
    t.join()
    assert bad == []
    assert not [p for p in os.listdir(tmp_path) if p != "p.md"], "no temp file left behind"


def test_the_callers_use_it():
    import inspect

    from src.agent.agent import Agent
    from src.services import profile_export

    assert "atomic_write_text(" in inspect.getsource(profile_export.export_profile_to_markdown)
    assert "atomic_write_text(" in inspect.getsource(Agent.update_working_memory_file)
