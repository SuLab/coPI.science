"""The landscaping prompt-set stamp and the BlackbirdBot extracts (spec §8.1-§8.2, D61).

Prompt-set tests build temporary directories; none needs the shipped prompt texts.
"""
import hashlib
import re
import shutil
import tomllib
from pathlib import Path

import pytest

from src.landscaping import prompt_set
from src.landscaping.criteria import CRITERIA_PATH, load_criteria
from src.landscaping.prompt_set import (
    JUDGING_FILES,
    PromptSetError,
    chat_prompt_matches,
    load_blackbird_extracts,
    load_prompt_set,
    prompt_set_from_texts,
    stamp,
    system_prompt,
)
from src.services.blackbird_rubric import load_rubric, render_rubric_markdown

SCOUT_HUB = Path("prompts/roles/scout_hub")
AGENT_SYSTEM = SCOUT_HUB / "agent-system.md"
PHASE4 = SCOUT_HUB / "phase4-thread-reply.md"

STANDARDS_HEADING = "## Opportunity Assessment Quality Standards"
PRACTICE_STARTS = ("**What this looks like in practice.**", "Or: an institution has chemical matter")
CONTRACT_HEADING = "### Concluding with an Opportunity Assessment: the sidecar"


def _make_set(root: Path, **texts: str) -> Path:
    """A stamped temp set: every judging file, chat.md and the shipped criteria file."""
    d = root / "landscaping"
    d.mkdir()
    for name in (*JUDGING_FILES, "chat.md"):
        (d / name).write_text(texts.get(name.removesuffix(".md"), f"{name} {{criteria_scope}}\n"))
    shutil.copy(CRITERIA_PATH, d / "criteria.toml")
    stamp("1.0.0", d)
    return d


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_headings_exist():
    agent, phase4 = _lines(AGENT_SYSTEM), _lines(PHASE4)
    assert STANDARDS_HEADING in agent
    assert "{rubric}" in agent[agent.index(STANDARDS_HEADING):]
    for start in PRACTICE_STARTS:
        assert any(line.startswith(start) for line in agent), start
    assert CONTRACT_HEADING in phase4


def test_extracts_nonempty_and_verbatim():
    ex = load_blackbird_extracts()
    agent = AGENT_SYSTEM.read_text(encoding="utf-8")
    phase4 = PHASE4.read_text(encoding="utf-8")

    rubric_md = render_rubric_markdown()
    assert ex.standards.endswith(rubric_md)
    head = ex.standards[: -len(rubric_md)]
    assert head.startswith(STANDARDS_HEADING) and head.strip() != STANDARDS_HEADING
    assert head in agent

    paragraphs = ex.practice.split("\n\n")
    assert [p[: len(s)] for p, s in zip(paragraphs, PRACTICE_STARTS, strict=True)] == list(PRACTICE_STARTS)
    for para in paragraphs:
        assert para in agent

    assert ex.contract.startswith(CONTRACT_HEADING) and ex.contract.strip() != CONTRACT_HEADING
    assert ex.contract in phase4

    role = tomllib.loads((SCOUT_HUB / "role.toml").read_text(encoding="utf-8"))
    assert ex.scout_hub_version == role["version"]
    assert re.fullmatch(r"[0-9a-f]{64}", ex.content_hash)


@pytest.mark.parametrize(("file", "remove"), [
    ("agent-system.md", STANDARDS_HEADING),
    ("agent-system.md", "{rubric}"),
    ("agent-system.md", PRACTICE_STARTS[0]),
    ("agent-system.md", PRACTICE_STARTS[1]),
    ("phase4-thread-reply.md", CONTRACT_HEADING),
])
def test_missing_heading_raises(tmp_path, monkeypatch, file, remove):
    for name in ("agent-system.md", "phase4-thread-reply.md", "role.toml"):
        shutil.copy(SCOUT_HUB / name, tmp_path / name)
    target = tmp_path / file
    text = target.read_text(encoding="utf-8")
    assert remove in text
    target.write_text(text.replace(remove, "removed"), encoding="utf-8")
    monkeypatch.setattr(prompt_set, "_SCOUT_HUB_DIR", tmp_path)
    with pytest.raises(PromptSetError):
        load_blackbird_extracts()


def test_hash_without_restamp_raises(tmp_path):
    d = _make_set(tmp_path)
    load_prompt_set(d)
    (d / "screen.md").write_text("edited without a stamp\n")
    with pytest.raises(PromptSetError, match="unrecorded edit"):
        load_prompt_set(d)


def test_judging_hashes_exclude_chat(tmp_path):
    d = _make_set(tmp_path)
    before = load_prompt_set(d)
    (d / "chat.md").write_text("a new chat prompt\n")
    stamp("1.0.1", d)
    after = load_prompt_set(d)
    assert set(after.judging_hashes) == set(JUDGING_FILES)
    assert after.judging_hashes == before.judging_hashes
    assert after.content_hash != before.content_hash
    assert after.version == "1.0.1"


def test_unrecorded_file_raises(tmp_path):
    d = _make_set(tmp_path)
    (d / "notes.md").write_text("never stamped\n")
    with pytest.raises(PromptSetError, match="notes.md"):
        load_prompt_set(d)
    (d / "notes.md").unlink()
    (d / "rerank.md").unlink()
    with pytest.raises(PromptSetError, match="rerank.md"):
        load_prompt_set(d)


def test_stamp_writes_file_hashes(tmp_path):
    d = _make_set(tmp_path)
    record = tomllib.loads((d / "set.toml").read_text())
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in d.iterdir() if p.name != "set.toml"
    }
    assert record["version"] == "1.0.0"
    assert record["files"] == expected
    assert "criteria.toml" in record["files"]

    ps = load_prompt_set(d)
    lines = "\n".join(f"{n} {expected[n]}" for n in sorted(expected))
    assert ps.content_hash == hashlib.sha256(lines.encode()).hexdigest()
    assert ps.judging_hashes == {n: expected[n] for n in JUDGING_FILES}
    assert set(ps.files) == set(expected)


def test_stamp_refuses_without_version_bump(tmp_path):
    d = _make_set(tmp_path)
    stamp("1.0.0", d)  # nothing changed: the same version is fine
    (d / "final.md").write_text("edited\n")
    with pytest.raises(PromptSetError):
        stamp("1.0.0", d)
    with pytest.raises(PromptSetError):
        load_prompt_set(d)
    stamp("1.1.0", d)
    assert load_prompt_set(d).version == "1.1.0"


def test_chat_prompt_matches(tmp_path):
    d = _make_set(tmp_path)
    assert chat_prompt_matches(d) is True
    (d / "chat.md").write_text("edited without a stamp\n")
    assert chat_prompt_matches(d) is False


def test_target_rubric_block_matches_load_rubric(tmp_path):
    d = _make_set(tmp_path, extract="{target_rubric}\n")
    rubric = load_rubric()
    out = system_prompt(
        "extract", ps=load_prompt_set(d), ex=load_blackbird_extracts(),
        criteria=load_criteria(), rubric=rubric,
    )
    by_key = {dim.key: dim.evidence for dim in rubric.dimensions}
    expected = list(by_key["scientific_credibility"]) + list(by_key["translational_path"])
    assert len(expected) == 11
    numbered = re.findall(r"^(\d+)\. (.+)$", out, re.M)
    assert numbered == [(str(i), item) for i, item in enumerate(expected, 1)]


def test_system_prompt_fills_placeholders_and_appends_extracts(tmp_path):
    d = _make_set(
        tmp_path,
        final="{criteria_scope}\n{criteria_vocab}\n{deal_breakers}\n{data_notice}\n",
        screen="{criteria_scope}\n",
    )
    ps, ex, criteria = load_prompt_set(d), load_blackbird_extracts(), load_criteria()
    final = system_prompt("final", ps=ps, ex=ex, criteria=criteria, rubric=load_rubric())
    assert "{criteria_scope}" not in final and criteria.scope_text in final
    assert " < ".join(criteria.evidence_grades) in final
    assert all(f"`{db.key}`" in final for db in criteria.deal_breakers)
    assert final.endswith("\n\n".join((ex.standards, ex.practice, ex.contract)))

    screen = system_prompt("screen", ps=ps, ex=ex, criteria=criteria, rubric=load_rubric())
    assert ex.practice not in screen
    with pytest.raises(PromptSetError):
        system_prompt("nonexistent", ps=ps, ex=ex, criteria=criteria, rubric=load_rubric())


def test_prompt_set_from_texts_hashes_like_the_loader(tmp_path):
    """A set rebuilt from an epoch's stored texts carries the hashes the files gave (§7.11)."""
    loaded = load_prompt_set(_make_set(tmp_path, screen="An invented screen prompt.\n"))
    texts = {name: loaded.files[name] for name in JUDGING_FILES}
    rebuilt = prompt_set_from_texts(loaded.version, texts)
    assert rebuilt.version == "1.0.0"
    assert rebuilt.judging_hashes == loaded.judging_hashes
    assert rebuilt.files == texts
    with pytest.raises(PromptSetError):
        prompt_set_from_texts("1.0.0", {k: v for k, v in texts.items() if k != "final.md"})
