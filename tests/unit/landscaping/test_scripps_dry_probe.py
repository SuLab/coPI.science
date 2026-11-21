from scripts import scripps_dry_probe as probe

S = "https://openalex.org/I123431417"
LA_JOLLA = "Department of Chemistry, The Scripps Research Institute, La Jolla, CA, USA"
JUPITER = "The Scripps Research Institute, Jupiter, FL 33458, USA"


def _auth(position, text, tagged=True):
    return {"author": {"display_name": "Avery Quillfeather"}, "author_position": position,
            "is_corresponding": False, "institutions": [{"id": S}],
            "raw_affiliation_strings": [text],
            "affiliations": [{"raw_affiliation_string": text, "institution_ids": [S] if tagged else []}]}


def _work(n, *auths):
    return {"id": f"https://openalex.org/W{n}", "authorships": list(auths)}


def test_summarize_counts_and_samples():
    works = [_work(1, _auth("last", LA_JOLLA)), _work(2, _auth("last", JUPITER)),
             _work(3, _auth("middle", LA_JOLLA)),
             _work(4, {"author_position": "last", "institutions": [{"id": "https://openalex.org/I999"}]})]
    out = probe.summarize(works)
    assert (out["openalex_works"], out["qualifying_works"], out["campus_rejected_works"]) == (4, 1, 1)
    assert out["accepted_addresses"] == [LA_JOLLA]
    assert out["rejected_addresses"] == [JUPITER]


def test_summarize_shows_raw_strings_of_an_untagged_rejection():
    jupiter_school = "Skaggs Graduate School, Jupiter, FL 33458, USA"
    out = probe.summarize([_work(1, _auth("last", jupiter_school, tagged=False))])
    assert (out["qualifying_works"], out["campus_rejected_works"]) == (0, 1)
    assert out["rejected_addresses"] == [jupiter_school]


def test_summarize_redacts_emails():
    text = LA_JOLLA + ". Electronic address: someone@example.org"
    out = probe.summarize([_work(1, _auth("last", text))])
    assert "@" not in out["accepted_addresses"][0] and "<email>" in out["accepted_addresses"][0]


def test_summarize_caps_the_sample():
    out = probe.summarize([_work(n, _auth("last", LA_JOLLA)) for n in range(30)], sample=20)
    assert out["qualifying_works"] == 30 and len(out["accepted_addresses"]) == 20
