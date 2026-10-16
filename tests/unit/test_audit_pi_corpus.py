"""scripts/audit_pi_corpus.py --baseline / --compare: the F6 before/after gate of the
2026-10-15 publication-handling design (R3.10), and the legacy per-PI classification. No
database, no network."""

import ast
import contextlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
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


# ---------------------------------------------------------------------------
# --baseline: the live resolve, the strict refetch and the exit code (no network: every
# call the script imports is monkeypatched).
# ---------------------------------------------------------------------------

_PI_ORCID = "0000-0002-1825-0097"
_ABSENT_ORCID = "0000-0001-5109-3700"


def _pi(*, anchored=(("101", "s1"),), openalex_dois=()):
    return audit.BaselinePi(
        user_id=str(uuid.uuid4()), orcid=_PI_ORCID, name="Rachel Green", institution=_JHU,
        in_use=len(anchored) + len(openalex_dois),
        stored_pmids=sorted(pmid for pmid, _ in anchored),
        anchored_rows=list(anchored), openalex_dois=list(openalex_dois),
    )


def _guards(monkeypatch):
    """Replace both OpenAlex budget guards; returns the list of awaited budget checks."""
    checked: list[bool] = []

    async def check():
        checked.append(True)

    monkeypatch.setattr(audit.openalex_budget, "check_inline_corpus_budget", check)
    monkeypatch.setattr(audit.openalex_budget, "bulk_requests", contextlib.nullcontext)
    return checked


def _gating_resolve(monkeypatch, calls=None):
    """A resolve_corpus that hands one anchored Hopkins record of the PI to the gate."""
    async def fake_resolve(orcid, name, institution):
        if calls is not None:
            calls.append(orcid)
        corpus._gate_records([_rec(1, year=2020, authors=[_PI])], {"1": {"s1"}},
                             name, institution, orcid)

    monkeypatch.setattr(audit, "resolve_corpus", fake_resolve)


def _raising(exc):
    async def fake(*args, **kwargs):
        raise exc

    return fake


def _fetching(records, seen=None):
    async def fake_fetch(pmids, *, strict=False):
        if seen is not None:
            seen.append((list(pmids), strict))
        by_pmid = {r["pmid"]: r for r in records}
        return [by_pmid[p] for p in pmids if p in by_pmid]

    return fake_fetch


def _no_openalex_inputs(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("no OpenAlex attribution lookup for a PI without openalex rows")

    monkeypatch.setattr(audit, "fetch_orcid_profile", unexpected)
    monkeypatch.setattr(audit, "_authorships", unexpected)


async def test_resolve_into_sets_the_gate_inputs_admissions_and_tenure(monkeypatch):
    checked = _guards(monkeypatch)
    _gating_resolve(monkeypatch)
    _no_openalex_inputs(monkeypatch)
    line = {"gate_inputs": None, "admitted": [], "paper_tenure_start": "unset"}
    await audit._resolve_into(line, _pi())
    assert checked == [True]
    assert [r["pmid"] for r in line["gate_inputs"]["records"]] == ["1"]
    assert line["gate_inputs"]["stages"] == {"1": ["s1"]}
    assert line["admitted"] == ["1"]
    assert line["paper_tenure_start"] == 2020
    assert "openalex" not in line


async def test_resolve_into_records_openalex_attribution_only_for_openalex_rows(monkeypatch):
    _guards(monkeypatch)
    _gating_resolve(monkeypatch)
    orcids: list[str] = []

    async def fake_profile(orcid):
        orcids.append(orcid)
        return {"organizations": ["Princeton University"]}

    async def fake_authorships(orcid, lookup):
        assert (orcid, lookup) == (_PI_ORCID, None)
        return {audit._doi_fold("10.1/a"): {"institutions": ["Princeton University"],
                                            "raw_affiliations": [], "orcid_in_metadata": False}}

    monkeypatch.setattr(audit, "fetch_orcid_profile", fake_profile)
    monkeypatch.setattr(audit, "_authorships", fake_authorships)
    line: dict = {}
    await audit._resolve_into(line, _pi(openalex_dois=["10.1/A", "10.1/b", "not a doi"]))
    assert orcids == [_PI_ORCID]
    assert line["openalex"]["organizations"] == ["Princeton University"]
    assert line["openalex"]["rows"] == [
        {"doi": "10.1/A", "attributed": True},
        {"doi": "10.1/b", "attributed": False},
        {"doi": "not a doi", "attributed": False},
    ]


async def test_resolve_into_stops_when_the_budget_guard_defers(monkeypatch):
    _guards(monkeypatch)

    async def deferring():
        raise RuntimeError("budget")

    monkeypatch.setattr(audit.openalex_budget, "check_inline_corpus_budget", deferring)
    monkeypatch.setattr(audit, "resolve_corpus", _raising(AssertionError("resolved anyway")))
    line: dict = {}
    with pytest.raises(RuntimeError, match="budget"):
        await audit._resolve_into(line, _pi())
    assert "gate_inputs" not in line


async def test_baseline_line_refetches_strictly_and_lists_missing_records(monkeypatch):
    _guards(monkeypatch)
    _gating_resolve(monkeypatch)
    seen: list = []
    monkeypatch.setattr(audit, "fetch_pubmed_records",
                        _fetching([_rec(101, authors=[_PI])], seen))
    line = await audit.baseline_line(_pi(anchored=(("101", "s1"), ("102", "s3"))),
                                     skip_resolve=False)
    assert seen == [(["101", "102"], True)]
    assert line["unrefetchable"] == ["102"]
    assert [r["pmid"] for r in line["rows"]] == ["101"]
    assert [r["pmid"] for r in line["row_records"]] == ["101"]
    assert line["resolve_error"] is None
    assert line["admitted"] == ["1"]


async def test_baseline_line_records_a_resolve_failure_without_gate_inputs(monkeypatch):
    _guards(monkeypatch)
    monkeypatch.setattr(audit, "fetch_pubmed_records", _fetching([_rec(101, authors=[_PI])]))
    monkeypatch.setattr(audit, "resolve_corpus", _raising(httpx.ConnectError("x")))
    line = await audit.baseline_line(_pi(openalex_dois=["10.1/a"]), skip_resolve=False)
    assert line["gate_inputs"] is None and line["openalex"] is None
    assert line["resolve_error"] == "x"
    assert [r["pmid"] for r in line["rows"]] == ["101"]


async def test_baseline_line_with_a_failed_refetch_has_no_rows(monkeypatch):
    _guards(monkeypatch)
    monkeypatch.setattr(audit, "fetch_pubmed_records", _raising(httpx.HTTPError("down")))
    monkeypatch.setattr(audit, "resolve_corpus", _raising(AssertionError("resolved anyway")))
    line = await audit.baseline_line(_pi(anchored=(("101", "s1"), ("102", "s3"))),
                                     skip_resolve=False)
    assert line["resolve_error"] == "PubMed refetch failed: down"
    assert line["rows"] == [] and line["row_records"] == [] and line["unrefetchable"] == []
    assert line["gate_inputs"] is None


async def test_a_refetch_bug_costs_one_pi_not_the_run(monkeypatch):
    """Strict mode re-raises any non-per-item failure (a KeyError from a bad response too);
    it becomes that PI's resolve_error instead of ending the baseline."""
    _guards(monkeypatch)
    monkeypatch.setattr(audit, "fetch_pubmed_records", _raising(KeyError("PubmedArticle")))
    line = await audit.baseline_line(_pi(anchored=(("101", "s1"),)), skip_resolve=True)
    assert line["resolve_error"] == "PubMed refetch failed: 'PubmedArticle'"
    assert line["rows"] == []


async def _baseline_exit(monkeypatch, pi, *, skip_resolve):
    async def fake_rows(db, orcids):
        return [pi]

    monkeypatch.setattr(audit, "get_session_factory", lambda: lambda: contextlib.nullcontext())
    monkeypatch.setattr(audit, "read_baseline_rows", fake_rows)
    return await audit._baseline([], skip_resolve)


async def test_baseline_exits_1_on_a_resolve_failure(monkeypatch, capsys):
    _guards(monkeypatch)
    monkeypatch.setattr(audit, "fetch_pubmed_records", _fetching([_rec(101, authors=[_PI])]))
    monkeypatch.setattr(audit, "resolve_corpus", _raising(httpx.ConnectError("x")))
    assert await _baseline_exit(monkeypatch, _pi(), skip_resolve=False) == 1
    assert f"# resolve_error: 1 PI(s): {_PI_ORCID}" in capsys.readouterr().err


async def test_baseline_exits_0_under_skip_resolve(monkeypatch, capsys):
    _guards(monkeypatch)
    monkeypatch.setattr(audit, "fetch_pubmed_records", _fetching([_rec(101, authors=[_PI])]))
    monkeypatch.setattr(audit, "resolve_corpus", _raising(AssertionError("resolved anyway")))
    assert await _baseline_exit(monkeypatch, _pi(), skip_resolve=True) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["resolve_error"] == audit.SKIPPED_RESOLVE


async def test_baseline_exits_1_under_skip_resolve_when_the_refetch_fails(monkeypatch):
    _guards(monkeypatch)
    monkeypatch.setattr(audit, "fetch_pubmed_records", _raising(httpx.HTTPError("down")))
    monkeypatch.setattr(audit, "resolve_corpus", _raising(AssertionError("resolved anyway")))
    assert await _baseline_exit(monkeypatch, _pi(), skip_resolve=True) == 1


# ---------------------------------------------------------------------------
# --compare: OpenAlex attribution changes, the E10 DOI guard and the coverage guards.
# ---------------------------------------------------------------------------


def _oa_authorship(*, institutions=(), raw=()):
    return {"institutions": list(institutions), "raw_affiliations": list(raw),
            "orcid_in_metadata": False}


def test_openalex_changes_split_new_failures_from_new_passes():
    fold = audit._doi_fold
    oa = {
        "organizations": ["Princeton University"],
        "authorships": {
            fold("10.1/lost"): _oa_authorship(raw=["Somewhere Else"]),
            fold("10.1/gained"): _oa_authorship(institutions=["Princeton University"]),
            fold("10.1/kept"): _oa_authorship(institutions=["Princeton University"]),
        },
        "rows": [
            {"doi": "10.1/lost", "attributed": True},
            {"doi": "10.1/gained", "attributed": False},
            {"doi": "10.1/kept", "attributed": True},
        ],
    }
    assert audit._openalex_changes(oa) == {
        "rows": 3, "fail_after": ["10.1/lost"], "pass_after_only": ["10.1/gained"],
    }
    assert audit._openalex_changes(None) is None


def test_authorship_for_a_doi_normalize_doi_rejects_is_none():
    authorships = {audit._doi_fold("10.1/a"): _oa_authorship()}
    assert audit._authorship_for(authorships, "not a doi") is None
    assert audit._authorship_for(authorships, "https://doi.org/10.1/A") == _oa_authorship()


def test_an_empty_compare_is_incomplete():
    lines, code = audit.summarize([], _CAPTURED)
    assert code == 1 and lines[-1] == "# VERDICT: INCOMPLETE"
    assert any(x.startswith("# no PI judged") for x in lines)


def test_an_orcid_missing_from_the_baseline_is_incomplete():
    lines, code = audit.summarize([_line()], _CAPTURED, missing=[_PI_ORCID])
    assert code == 1 and lines[-1] == "# VERDICT: INCOMPLETE"
    assert f"# --orcid not in the baseline: {_PI_ORCID}" in lines
    _lines, code = audit.summarize([_line()], _CAPTURED)
    assert code == 0


def test_stop_wins_over_a_missing_orcid():
    riser = _line("Grace Hopper", in_use=40, rise=11, rise_trips=True)
    lines, code = audit.summarize([riser], _CAPTURED, missing=[_ABSENT_ORCID])
    assert code == 2 and lines[-1] == "# VERDICT: STOP FOR OWNER REVIEW"
    assert f"# --orcid not in the baseline: {_ABSENT_ORCID}" in lines


def test_compare_with_an_orcid_absent_from_the_baseline_exits_1(tmp_path, capsys):
    path = tmp_path / "baseline.jsonl"
    path.write_text(json.dumps(_base("Ada Lovelace", gate={"records": [], "stages": {}}))
                    + "\n", encoding="utf-8")
    assert audit._compare(str(path), []) == 0
    capsys.readouterr()
    assert audit._compare(str(path), [_ABSENT_ORCID]) == 1
    err = capsys.readouterr().err
    assert f"# --orcid not in the baseline: {_ABSENT_ORCID}" in err
    assert "# no PI judged" in err


def test_compare_of_an_empty_file_exits_1(tmp_path, capsys):
    path = tmp_path / "baseline.jsonl"
    path.write_text("", encoding="utf-8")
    assert audit._compare(str(path), []) == 1
    assert "# no PI judged" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Legacy mode: ambiguous and incomplete-list records are counted.
# ---------------------------------------------------------------------------


async def test_legacy_mode_counts_ambiguous_and_incomplete_list(monkeypatch, capsys):
    user = SimpleNamespace(id=uuid.uuid4(), orcid=_PI_ORCID, name="Rachel Green",
                           institution=_JHU)
    stored = [SimpleNamespace(pmid=str(p), title=f"Paper {p}") for p in (1, 2, 3)]
    records = {
        "1": _rec(1, authors=[_PI]),
        "2": _rec(2, authors=[_author("Green", "Rachel", "R", affs=["X"]),
                              _author("Green", "Rachel", "R", affs=["Y"])]),
        "3": {**_rec(3, authors=[_author("Smith", "Ann", "A")]), "author_list_complete": False},
    }

    async def fake_stored(db, user_id):
        assert user_id == user.id
        return stored

    async def fake_refetch(pmids):
        return {p: records[p] for p in pmids}

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [user]

    class _Db:
        async def execute(self, statement):
            return _Result()

    monkeypatch.setattr(audit, "load_stored_publications", fake_stored)
    monkeypatch.setattr(audit, "refetch_pmids", fake_refetch)
    monkeypatch.setattr(audit, "get_session_factory",
                        lambda: lambda: contextlib.nullcontext(_Db()))

    assert await audit._run([], skip_resolve=True) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["classification"]["strong"] == 1
    assert report["classification"]["ambiguous"] == 1
    assert report["classification"]["incomplete_list"] == 1
    assert report["classification"]["no_surname_author"] == 0
    assert ("# classification: strong=1 bare_initial_only=0 no_surname_author=0 "
            "ambiguous=1 incomplete_list=1") in captured.err
