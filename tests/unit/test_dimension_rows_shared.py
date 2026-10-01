"""AP-10: the directory rows ARE the detail rows (the detail builder already emits
`rationale` and feeds the chat record, so the record does not change); the directory
only gains a key its template does not read."""
from types import SimpleNamespace

import pytest

from src.services import directory
from src.services.assessment_detail import build_dimension_rows
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.rubric_revisions import resolve_revision

_ROWS = [
    SimpleNamespace(
        scores={"Team_Executability": 4, "novelty": 3.5, "legacy_dim": 2, "bogus": "x"},
        dimension_rationales={"team_executability": "strong team"},
        rubric_version=None, rubric_content_hash=None,
    ),
    SimpleNamespace(
        scores={"Team_Executability": 4, "novelty": 3.5, "legacy_dim": 2, "bogus": "x"},
        dimension_rationales={"team_executability": "strong team"},
        rubric_version=RUBRIC_VERSION, rubric_content_hash=RUBRIC_CONTENT_HASH,
    ),
]


@pytest.mark.parametrize("row", _ROWS)
def test_directory_rows_equal_detail_rows(row):
    rows, revision = directory._assessment_dimension_rows(row)
    assert rows == build_dimension_rows(row, revision)


@pytest.mark.parametrize("row", _ROWS)
def test_directory_rows_keep_every_old_key_and_value(row):
    rows, _revision = directory._assessment_dimension_rows(row)
    old = [{k: r[k] for k in ("key", "title", "score", "weight", "weight_note", "pct")} for r in rows]
    assert old == _frozen_directory_rows(row)


def _frozen_directory_rows(row):
    """Today's directory builder body (directory.py at PHASE_BASE), verbatim."""
    scores = row.scores if isinstance(row.scores, dict) else {}
    normalized_scores = {key.strip().lower(): value for key, value in scores.items() if isinstance(key, str)}
    revision, _p = resolve_revision(row.rubric_version, row.rubric_content_hash)

    def _score_value(raw):
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None

    def _pct(value):
        if revision is None:
            return None
        if value is None:
            return 0.0
        return min(100.0, max(0.0, value / revision.scale_max * 100.0))

    rows, named = [], set()
    if revision is not None:
        for dim in revision.dimensions:
            value = _score_value(normalized_scores.get(dim.key))
            named.add(dim.key)
            rows.append({"key": dim.key, "title": dim.title, "score": value, "weight": dim.weight,
                         "weight_note": dim.weight_note, "pct": _pct(value)})
    for key in sorted(normalized_scores):
        if key in named:
            continue
        value = _score_value(normalized_scores[key])
        if value is None:
            continue
        rows.append({"key": key, "title": key.replace("_", " "), "score": value, "weight": None,
                     "weight_note": None, "pct": _pct(value)})
    return rows
