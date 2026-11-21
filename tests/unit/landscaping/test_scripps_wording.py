"""No Hopkins words where the model or a reviewer reads landscaping text (Scripps spec §6.2)."""
import re
from pathlib import Path
from types import SimpleNamespace

from src.landscaping.stages import final, triage
from src.services.landscape_chat_record import DOC_VERDICT, LEGENDS
from src.web.help_pages import HELP_PAGES

ROOT = Path(__file__).resolve().parents[3]
HOPKINS = re.compile(r"hopkins|\bjhu\b|baltimore", re.IGNORECASE)


def _scanned() -> list[Path]:
    files = [p for p in (ROOT / "src/landscaping").rglob("*.py")
             if p != ROOT / "src/landscaping/sources/jhtv.py"]
    files += [ROOT / "src/services/landscape_chat_record.py",
              ROOT / "src/services/landscape_view.py",
              ROOT / "templates/help/pages/landscape.html",
              ROOT / "templates/help/tutorials/manager/11-landscape.html",
              ROOT / "templates/help/tutorials/reviewer/09-landscape.html",
              ROOT / "docs/specs/2026-11-20-landscaping-prompts.md"]
    files += sorted((ROOT / "templates/landscape").glob("*.html"))
    files += sorted((ROOT / "templates/workspace").glob("landscape*.html"))
    return files


def test_no_hopkins_words_in_landscaping_text():
    hits = [f"{p.relative_to(ROOT)}:{n}: {line.strip()}"
            for p in _scanned()
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if HOPKINS.search(line)]
    assert len(_scanned()) > 70   # 77 files on 2026-11-20
    assert not hits, "\n".join(hits)


def test_glossary_opportunity_entry_names_scripps():
    text = (ROOT / "templates/help/pages/glossary.html").read_text(encoding="utf-8")
    entry = text.split('id="term-opportunity"', 1)[1].split("</dd>", 1)[0]
    assert "Scripps Research" in entry and not HOPKINS.search(entry)


def test_help_registry_landscape_summary():
    summary = next(p.summary for p in HELP_PAGES if p.slug == "landscape")
    assert "Scripps Research" in summary and not HOPKINS.search(summary)


def test_model_visible_labels():
    dossier = triage.triage_dossier({"idea_type": "biology", "jhu_group_count": 2}, {}, [])
    assert "- Independent Scripps Research groups: 2" in dossier
    facts = final._idea_facts(SimpleNamespace(
        idea_type="biology", targets=None, direction=None, agent=None, disease=None,
        modality_variants=None, stage=None, evidence_grade=None, jhu_group_count=3,
        outside_group_count=None, evidence_summary=None, data_flags=None))
    assert facts["scripps_group_count"] == 3 and "jhu_group_count" not in facts
    assert "Scripps Research labs" in LEGENDS[DOC_VERDICT]
