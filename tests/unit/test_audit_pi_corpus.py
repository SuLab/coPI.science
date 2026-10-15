"""scripts/audit_pi_corpus.py --baseline / --compare: the F6 before/after gate of the
2026-10-15 publication-handling design (R3.10). No database, no network."""

import ast
import json
import uuid
from pathlib import Path

import pytest

from scripts import audit_pi_corpus as audit
from src.services import corpus
from tests.unit.test_corpus import _PI, _rec, _wire

_JHU = "Johns Hopkins University"
_CAPTURED = "2026-10-16T00:00:00+00:00"


def _author(last=None, fore=None, initials=None, collective=None, affs=()):
    return {"last": last, "fore": fore, "initials": initials, "collective": collective,
            "affiliations": list(affs)}


def _record(pmid, *authors, pub_types=("Journal Article",), **extra):
    return {"pmid": str(pmid), "title": f"Paper {pmid}", "year": 2020,
            "pub_types": list(pub_types), "authors": list(authors), **extra}


def _base(name, *, rows=(), records=(), gate=None, admitted=(), stored=(), in_use=10,
          start=None, error=None, user_id=None):
    return {
        "format": audit.BASELINE_FORMAT, "captured_at": _CAPTURED,
        "user_id": user_id or str(uuid.uuid4()), "orcid": "0000-0002-1825-0097",
        "name": name, "institution": _JHU, "in_use": in_use, "stored_pmids": sorted(stored),
        "rows": list(rows), "row_records": list(records), "unrefetchable": [],
        "gate_inputs": gate, "admitted": list(admitted), "paper_tenure_start": start,
        "openalex": None, "resolve_error": error,
    }


def _line(name="Ada Lovelace", **over):
    line = {
        "format": audit.COMPARE_FORMAT, "user_id": str(uuid.uuid4()),
        "orcid": "0000-0002-1825-0097", "name": name, "in_use": 100, "evaluated": 0,
        "unrefetchable": [], "dropped": [], "still_failing": [], "recovered": [],
        "newly_admitted": [], "newly_admitted_authors": {}, "no_longer_admitted": [],
        "rise": 0, "rise_trips": False, "paper_tenure_start": None, "openalex": None,
        "incomplete": None,
    }
    line.update(over)
    return line


def test_gate_verdict_names_the_outcome():
    green = _author("Green", "Rachel", "R")
    verdict = audit.gate_verdict
    assert verdict(_record(1, green), ["s1"], "Rachel Green", _JHU, None) == "kept"
    assert verdict(_record(2, _author("Smith", "Ann", "A")), ["s2"], "Rachel Green", _JHU,
                   None) == "flagged:no_individual_author_match"
    assert verdict(_record(3, green, pub_types=["Editorial"]), ["s1"], "Rachel Green", _JHU,
                   None) == "dropped:excluded_type"
    assert verdict(_record(4, _author(collective="GTEx Consortium")), ["s1"], "Rachel Green",
                   _JHU, None) == "dropped:consortium"


async def test_capture_gate_inputs_records_the_gate_call_and_restores_it(monkeypatch):
    original = corpus._gate_records
    _wire(monkeypatch, orcid_works=[{"pmid": "1"}], s3=["2"],
          records=[_rec(1, authors=[_PI]), _rec(2, authors=[_PI])])
    with audit.capture_gate_inputs() as captured:
        await corpus.resolve_corpus("0000-0001-2345-6789", "Rachel Green", _JHU)
    assert corpus._gate_records is original
    assert [r["pmid"] for r in captured["records"]] == ["1", "2"]
    assert captured["stages"] == {"1": ["s1"], "2": ["s3"]}
    assert "abstract" not in captured["records"][0]
    with pytest.raises(RuntimeError, match="boom"):
        with audit.capture_gate_inputs():
            raise RuntimeError("boom")
    assert corpus._gate_records is original


def test_trim_keeps_only_the_gate_keys():
    author = {**_author("Green", "Rachel", "R"), "valid": False, "position": 1, "suffix": None}
    record = {**_record(1, author), "abstract": "A.", "coi_statement": "None.", "journal": "J",
              "doi": "10.1/x", "author_list_complete": False, "update_of": [], "updated_in": ["2"]}
    trimmed = audit._trim(record)
    assert set(trimmed) == {"pmid", "title", "year", "pub_types", "authors",
                            "author_list_complete", "update_of", "updated_in"}
    assert trimmed["authors"] == [{"last": "Green", "fore": "Rachel", "initials": "R",
                                   "collective": None, "affiliations": [], "valid": False}]
    plain = audit._trim(_record(2, _author("Green", "Rachel")))
    assert "author_list_complete" not in plain
    assert "valid" not in plain["authors"][0]


def test_compare_counts_a_drop_a_recovery_and_a_new_admission():
    green = _base(
        "Rachel Green", rows=[{"pmid": "11", "provenance": "s1", "verdict": "kept"}],
        records=[_record(11, _author("Green", "R Lara", "RL"))],
        gate={"records": [], "stages": {}}, stored=["11"],
    )
    kjaer = _base(
        "Chi Kjaer",
        rows=[{"pmid": "21", "provenance": "s1", "verdict": "flagged:no_individual_author_match"}],
        records=[_record(21, _author("Kjær", "Chi", "C"))],
        gate={"records": [_record(22, _author("Kjær", "Chi", "C"))], "stages": {"22": ["s3"]}},
        stored=["21"],
    )
    out_green, out_kjaer = audit.compare_pi(green), audit.compare_pi(kjaer)
    assert out_green["dropped"] == [
        {"pmid": "11", "before": "kept", "after": "flagged:no_individual_author_match"}
    ]
    assert out_green["evaluated"] == 1 and out_green["recovered"] == []
    assert out_kjaer["recovered"] == [
        {"pmid": "21", "before": "flagged:no_individual_author_match", "after": "kept"}
    ]
    assert out_kjaer["newly_admitted"] == ["22"]
    assert out_kjaer["rise"] == 1 and out_kjaer["rise_trips"] is False


def test_drop_limit_is_more_than_one_percent():
    def run(n):
        drops = [{"pmid": str(k), "before": "kept", "after": "flagged:no_individual_author_match"}
                 for k in range(n)]
        return audit.summarize([_line(evaluated=100, dropped=drops)], _CAPTURED)

    lines, code = run(1)
    assert code == 0 and lines[-1] == "# VERDICT: PASS"
    assert lines[1].endswith("1 of 100 (1.00%), stop above 1%: PASS")
    lines, code = run(2)
    assert code == 2 and lines[-1] == "# VERDICT: STOP FOR OWNER REVIEW"


@pytest.mark.parametrize(("in_use", "rise", "trips"), [
    (40, 11, True), (40, 10, False), (100, 20, False), (0, 11, True), (8, 9, False),
])
def test_rise_limit_needs_both_conditions(in_use, rise, trips):
    assert audit.rise_trips(in_use, rise) is trips


def test_a_pi_without_gate_inputs_is_incomplete():
    line = audit.compare_pi(_base("Ada Lovelace", error="corpus stage s2_openalex failed: 503"))
    assert line["incomplete"] == "no gate inputs: corpus stage s2_openalex failed: 503"
    lines, code = audit.summarize([line], _CAPTURED)
    assert code == 1 and lines[-1] == "# VERDICT: INCOMPLETE"
    assert ("# incomplete PI(s): 1: Ada Lovelace "
            "(no gate inputs: corpus stage s2_openalex failed: 503)") in lines
    riser = _line("Grace Hopper", in_use=40, rise=11, rise_trips=True)
    lines, code = audit.summarize([line, riser], _CAPTURED)
    assert code == 2 and lines[-1] == "# VERDICT: STOP FOR OWNER REVIEW"
    assert "#   Grace Hopper (0000-0002-1825-0097): +11 on 40 in use" in lines


def test_compare_refuses_an_unknown_format(tmp_path, capsys):
    path = tmp_path / "baseline.jsonl"
    path.write_text(json.dumps({"format": "audit_pi_corpus.baseline/0"}) + "\n", encoding="utf-8")
    assert audit._compare(str(path), []) == 1
    assert "unknown baseline format: audit_pi_corpus.baseline/0" in capsys.readouterr().err


def test_compare_keeps_the_last_line_per_user():
    user_id = str(uuid.uuid4())
    first = _base("Ada Lovelace", user_id=user_id, in_use=3)
    last = _base("Ada Lovelace", user_id=user_id, in_use=7)
    (kept,) = audit.read_baseline([json.dumps(first), "", json.dumps(last)])
    assert kept["in_use"] == 7


def test_tenure_start_change_is_reported():
    lines, _code = audit.summarize(
        [_line(paper_tenure_start={"before": 2011, "after": 2013})], _CAPTURED)
    assert "# paper-derived tenure start changed for 1 PI(s): Ada Lovelace 2011 -> 2013" in lines


def test_compare_names_the_matched_author_of_new_admissions():
    base = _base("José Garcia", gate={
        "records": [_record(31, _author("Garcia-Lopez", "José", "J"))],
        "stages": {"31": ["s1"]},
    })
    line = audit.compare_pi(base)
    assert line["newly_admitted"] == ["31"]
    assert line["newly_admitted_authors"] == {"31": "Garcia-Lopez, José"}


_PINNED = {
    ("scripts._bulk_enqueue", "pi_population"),
    ("scripts.repair_pi_corpus", "classify_excluded_type"),
    ("scripts.repair_pi_corpus", "load_stored_publications"),
    ("scripts.repair_pi_corpus", "normalize_title"),
    ("scripts.repair_pi_corpus", "refetch_pmids"),
    ("src.database", "get_session_factory"),
    ("src.models", "User"),
    ("src.models.publication", "PROVENANCE_OPENALEX"),
    ("src.models.publication", "Publication"),
    ("src.services", "corpus"),
    ("src.services", "openalex_budget"),
    ("src.services.corpus", "CorpusStageError"),
    ("src.services.corpus", "_dedupe_and_rank"),
    ("src.services.corpus", "_gate_records"),
    ("src.services.corpus", "_match_pi_author_detail"),
    ("src.services.corpus", "resolve_corpus"),
    ("src.services.jhu_rules", "derive_start_from_papers"),
    ("src.services.job_queue", "JobDeferred"),
    ("src.services.non_pubmed_works", "_attributed"),
    ("src.services.non_pubmed_works", "_authorships"),
    ("src.services.orcid", "fetch_orcid_profile"),
    ("src.services.pubmed", "PubMedParseError"),
    ("src.services.pubmed", "_doi_fold"),
    ("src.services.pubmed", "fetch_pubmed_records"),
    ("src.services.pubmed", "normalize_doi"),
}
_COMPARE_ONLY = {("src.services.corpus", "_match_pi_author_full")}


def _project_imports(nodes):
    found = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] in ("src", "scripts") for a in node.names)
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in ("src", "scripts"):
            found |= {(node.module, alias.name) for alias in node.names}
    return found


def test_baseline_imports_only_names_the_pre_phase3_image_has():
    tree = ast.parse(Path(audit.__file__).read_text(encoding="utf-8"))
    compare = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "compare_pi")
    inside = {id(node) for node in ast.walk(compare)}
    outside = [node for node in ast.walk(tree) if id(node) not in inside]
    assert _project_imports(outside) <= _PINNED
    assert _project_imports(ast.walk(compare)) == _COMPARE_ONLY
