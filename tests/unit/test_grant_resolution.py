"""RePORTER identity rule and record collapse (spec 2026-10-05 §6.1)."""
from src.services import grant_resolution as gr
from src.services.person_names import parse_person_name

JHU = {"org_name": "JOHNS HOPKINS UNIVERSITY"}


def pi(pid, first, last):
    return {"profile_id": pid, "is_contact_pi": True, "first_name": first, "last_name": last}


def row(core, fy, pid, org=JHU, amount=100, first="Peng", last="Wu", sub=None, code=None, pis=None):
    code = code or core[:3]
    return {"core_project_num": core, "project_num": f"5{core}-0{fy%10}", "fiscal_year": fy,
            "organization": org, "award_amount": amount, "subproject_id": sub, "activity_code": code,
            "project_title": f"T {core}", "phr_text": "p", "terms": "t", "agency_ic_admin": {"code": "AI"},
            "funding_mechanism": "Non-SBIR/STTR", "project_start_date": f"{fy}-03-01T00:00:00",
            "project_end_date": f"{fy+2}-02-28T00:00:00",
            "principal_investigators": pis if pis is not None else [pi(pid, first, last)]}


def _resolve(rows, name, links, corpus):
    found = gr.find_candidates(rows, parse_person_name(name))
    return gr.resolve_identity(found, links, corpus)


def test_shared_core_names_only_the_pi():
    """Zavala and Coppens on one JHU award: before, every PI on a PMID-linked JHU core was
    accepted; now only the name-matched entry is a candidate."""
    shared = row("R01AI000001", 2020, None,
                 pis=[pi(111, "Fidel", "Zavala"), pi(222, "Isabelle", "Coppens")])
    result = _resolve([shared], "Fidel Zavala", {"R01AI000001": {"9"}}, {"9"})
    assert result.status == "resolved" and result.accepted_profile_ids == (111,)
    assert [c.profile_id for c in result.candidates] == [111]


def test_linking_uses_only_the_candidates_own_cores():
    rows = [row("R01AA000001", 2020, 111, first="Peng"),
            row("R01BB000002", 2020, 999, first="Mei", last="Lin")]
    result = _resolve(rows, "Peng Wu", {"R01BB000002": {"5"}}, {"5"})
    assert result.status == "unconfirmed" and result.accepted_profile_ids == ()


def test_a_single_unlinked_candidate_is_unconfirmed():
    result = _resolve([row("R21CC000003", 2022, 333)], "Peng Wu", {}, set())
    assert result.status == "unconfirmed" and [c.profile_id for c in result.candidates] == [333]


def test_two_linked_candidates_are_held():
    rows = [row("R01AA000001", 2020, 111), row("R01BB000002", 2020, 222, first="P.")]
    result = _resolve(rows, "Peng Wu", {"R01AA000001": {"1"}, "R01BB000002": {"2"}}, {"1", "2"})
    assert result.status == "held" and result.accepted_profile_ids == (111, 222)


def test_two_unlinked_or_zero_candidates_are_no_match():
    rows = [row("R01AA000001", 2020, 111), row("R01BB000002", 2020, 222)]
    assert _resolve(rows, "Peng Wu", {}, set()).status == "no_match"
    assert _resolve([], "Peng Wu", {}, set()).status == "no_match"


def test_first_names_must_agree():
    rows = [row("R01AA000001", 2020, 111, first="Vlad")]
    assert _resolve(rows, "Victor Wu", {}, set()).status == "no_match"


def test_accented_and_particle_surnames_match():
    r1 = row("R01AA000001", 2020, 111, first="Gabriel", last="Garcia Marquez")
    assert _resolve([r1], "Gabriel García Márquez", {"R01AA000001": {"1"}}, {"1"}).status == "resolved"
    r2 = row("R01BB000002", 2020, 222, first="Iris", last="van 't Erve")
    assert _resolve([r2], "Iris van 't Erve", {"R01BB000002": {"2"}}, {"2"}).status == "resolved"


def test_non_jhu_rows_give_no_candidates():
    other = row("R01AA000001", 2020, 111, org={"org_name": "UNIVERSITY OF ELSEWHERE"})
    assert _resolve([other], "Peng Wu", {"R01AA000001": {"1"}}, {"1"}).status == "no_match"


def test_candidate_json_shape():
    c = gr.Candidate(111, "Peng Wu", frozenset({"R01AA000001"}), frozenset({"2", "1"}))
    assert c.as_json() == {"id": 111, "name_on_award": "Peng Wu", "linked": True,
                           "linking_pmids": ["1", "2"]}


def test_records_carry_the_name_on_award_and_per_grant_evidence():
    recs, _ = gr.filter_and_collapse([row("R01AA000001", 2020, 111)], {111}, tenure_start=None)
    assert recs[0].name_on_award == "Peng Wu"
    ev = gr.grant_evidence(recs[0], {"R01AA000001": {"7", "8"}}, {"8"}, "pmid_link")
    assert ev == {"matched_profile_id": 111, "name_on_award": "Peng Wu", "linking_pmids": ["8"],
                  "rule": "pmid_link"}


def test_moved_institution_award_keeps_only_jhu_years():
    rows = [row("R01AA000001", 2016, 111, org={"org_name": "UNIVERSITY OF ELSEWHERE"}),
            row("R01AA000001", 2018, 111), row("R01AA000001", 2019, 111)]
    recs, mode = gr.filter_and_collapse(rows, {111}, tenure_start=2018)
    assert mode == "org_and_year" and len(recs) == 1
    assert (recs[0].first_fy, recs[0].last_fy, recs[0].total_award_in_tenure) == (2018, 2019, 200)


def test_pre_tenure_fiscal_years_excluded_even_at_jhu():
    rows = [row("R01AA000001", 2015, 111), row("R01AA000001", 2019, 111)]
    recs, _ = gr.filter_and_collapse(rows, {111}, tenure_start=2018)
    assert recs[0].first_fy == 2019


def test_no_tenure_is_org_only_mode():
    rows = [row("R01AA000001", 2010, 111)]
    recs, mode = gr.filter_and_collapse(rows, {111}, tenure_start=None)
    assert mode == "org_only" and len(recs) == 1


def test_supplement_rows_not_double_summed():
    a = row("R01AA000001", 2020, 111, amount=100)
    b = dict(a)  # same project_num → duplicate row
    recs, _ = gr.filter_and_collapse([a, b], {111}, tenure_start=None)
    assert recs[0].total_award_in_tenure == 100
