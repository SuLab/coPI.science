from src.landscaping.institutions import (
    BALTIMORE_OPENALEX_IDS,
    EXCLUDED_OPENALEX_IDS,
    is_baltimore_affiliation,
    qualifies_openalex,
)


def test_baltimore_ids_match_spec():
    assert BALTIMORE_OPENALEX_IDS == frozenset({
        "I145311948", "I2799853436", "I4210150714", "I4210164401", "I2802697821",
        "I4392738222", "I4391767829", "I4210092215", "I4210151848", "I4391012584",
        "I4394709191", "I4389425327", "I4405260374", "I4392738135", "I4387152773",
        "I4388891755", "I4210120405", "I4210129832", "I4210114877", "I4405267562",
        "I1298154867",
    })
    assert len(BALTIMORE_OPENALEX_IDS) == 21
    assert not BALTIMORE_OPENALEX_IDS & EXCLUDED_OPENALEX_IDS


def test_matcher():
    assert is_baltimore_affiliation("Johns Hopkins University School of Medicine")
    assert is_baltimore_affiliation("Kennedy Krieger Institute")
    assert is_baltimore_affiliation("Jhpiego")
    assert not is_baltimore_affiliation("Johns Hopkins University Applied Physics Laboratory, Laurel, MD")
    assert not is_baltimore_affiliation("Johns Hopkins All Children's Hospital, St. Petersburg")


def _auth(position="middle", corresponding=False, inst_ids=(), raw=(), name="Avery Quillfeather"):
    return {
        "author": {"display_name": name, "orcid": None},
        "raw_author_name": name,
        "author_position": position,
        "is_corresponding": corresponding,
        "institutions": [{"id": f"https://openalex.org/{i}"} for i in inst_ids],
        "raw_affiliation_strings": list(raw),
    }


def _work(*auths):
    return {"id": "https://openalex.org/W1", "authorships": [_auth("first", inst_ids=["I999"]), *auths]}


def test_qualifies_openalex():
    last = _auth("last", inst_ids=["I145311948"])
    assert qualifies_openalex(_work(last)) == (True, "last", last)

    corr = _auth("middle", corresponding=True, inst_ids=["I4210164401"])
    assert qualifies_openalex(_work(corr, _auth("last", inst_ids=["I999"]))) == (True, "corresponding", corr)

    apl_only = _auth("last", inst_ids=["I2802946424"])
    assert qualifies_openalex(_work(apl_only)) == (False, None, None)

    jhu_text = ["Department of Medicine, Johns Hopkins University School of Medicine, Baltimore, MD"]
    raw_only = _auth("last", raw=jhu_text)
    assert qualifies_openalex(_work(raw_only)) == (True, "last", raw_only)
    raw_with_other_inst = _auth("last", inst_ids=["I999"], raw=jhu_text)
    assert qualifies_openalex(_work(raw_with_other_inst)) == (False, None, None)

    collective = _auth("last", inst_ids=["I145311948"], name="Invented Cohort Study Consortium")
    assert qualifies_openalex(_work(collective)) == (False, None, None)
