from src.services import grant_resolution as gr

JHU = {"org_name": "JOHNS HOPKINS UNIVERSITY"}


def row(core, fy, pid, org=JHU, amount=100, first="Peng", last="Wu", sub=None, code=None):
    code = code or core[:3]
    return {"core_project_num": core, "project_num": f"5{core}-0{fy%10}", "fiscal_year": fy,
            "organization": org, "award_amount": amount, "subproject_id": sub, "activity_code": code,
            "project_title": f"T {core}", "phr_text": "p", "terms": "t", "agency_ic_admin": {"code": "AI"},
            "funding_mechanism": "Non-SBIR/STTR", "project_start_date": f"{fy}-03-01T00:00:00",
            "project_end_date": f"{fy+2}-02-28T00:00:00",
            "principal_investigators": [{"profile_id": pid, "is_contact_pi": True, "first_name": first, "last_name": last}]}


def test_name_collision_resolved_by_pmid_link():
    rows = [row("R01AA000001", 2020, 111), row("R01BB000002", 2020, 222)]
    links = {"R01AA000001": {"1", "2"}, "R01BB000002": {"9"}}
    ids, ev = gr.resolve_profile_ids(rows, corpus_pmids={"2"}, links=links, first_name="Peng")
    assert ids == {111} and ev["pmid_linked"] == [111] and 222 in ev["rejected"]


def test_unique_jhu_candidate_with_exact_first_name_accepted_without_links():
    rows = [row("R21CC000003", 2022, 333)]
    ids, ev = gr.resolve_profile_ids(rows, corpus_pmids=set(), links={}, first_name="Peng")
    assert ids == {333} and ev["unique_name"] == [333]


def test_two_candidates_no_links_accepts_nobody():
    rows = [row("R01AA000001", 2020, 111), row("R01BB000002", 2020, 222)]
    ids, _ = gr.resolve_profile_ids(rows, corpus_pmids=set(), links={}, first_name="Peng")
    assert ids == set()


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


def test_grant_titles_exclude_training_codes_and_sort_recent_first():
    rows = [row("T32DD000004", 2024, 111, code="T32"), row("R01AA000001", 2019, 111), row("R21CC000003", 2023, 111)]
    recs, _ = gr.filter_and_collapse(rows, {111}, tenure_start=None)
    assert gr.derive_grant_titles(recs) == ["T R21CC000003", "T R01AA000001"]
