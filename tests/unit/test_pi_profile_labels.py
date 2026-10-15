"""The PI-page paper label (P9) and the generation-note labels (R4.21)."""
import ast
from pathlib import Path

from src.models import Publication
from src.routers.workspace.pi_profile import _paper_label
from src.services.profile_review import GENERATION_NOTE_LABELS, GENERATION_NOTE_STAFF_TEXT

PIPELINE = Path(__file__).resolve().parents[2] / "src" / "services" / "profile_pipeline.py"
ROUTINE = {"start", "complete", *(f"step{i}" for i in range(1, 11))}


def test_paper_label():
    assert _paper_label(Publication(pmid="1", doi="10.1/x", title="T")) == "PMID 1"
    assert _paper_label(Publication(pmid=None, doi="10.1/x", title="T")) == "DOI 10.1/x"
    assert _paper_label(Publication(pmid=None, doi=None, title="Some title")) == '"Some title"'


def test_every_pipeline_step_is_labelled_or_routine():
    # A step with no label is hidden from staff; a label with no step is dead.
    steps = {
        node.args[0].value for node in ast.walk(ast.parse(PIPELINE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "progress" and node.args
        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
    }
    assert "corpus_rechecked" in steps, "control: the scan reached the pipeline's steps"
    assert steps - ROUTINE == set(GENERATION_NOTE_LABELS)
    assert set(GENERATION_NOTE_STAFF_TEXT) <= set(GENERATION_NOTE_LABELS)
