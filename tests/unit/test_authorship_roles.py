"""`authorship_roles` on the authorship shapes `tests/unit/test_industry_sources_parsers.py`
builds, with the expected values written out: comparing with `openalex_industry`, which
now delegates here, would be circular."""
from src.services.authorship_roles import ROLE_RANK, at_institution, is_individual, role
from src.services.industry_sources import JHU_OPENALEX_IDS
from src.services.jhu_rules import is_hopkins_affiliation

PI = "https://orcid.org/0000-0002-2214-0114"


def _pi(inst_ids, raw=None, pos="last"):
    """The PI authorship of the parser tests' `work` builder."""
    return {"author": {"orcid": PI},
            "institutions": [{"id": f"https://openalex.org/{i}", "display_name": "JHU", "type": "education"}
                             for i in inst_ids],
            "raw_affiliation_strings": raw or [], "author_position": pos, "is_corresponding": pos == "last"}


COMPANY = {"author": {"orcid": None},
           "institutions": [{"id": "https://openalex.org/I1", "display_name": "Pfizer", "type": "company"}],
           "author_position": "middle", "is_corresponding": False}
FILLER = {"author": {"orcid": None}, "institutions": [], "author_position": "middle"}
SHARED_FIRST = {"author": {"orcid": None}, "author_position": "first", "is_corresponding": False,
                "institutions": [{"id": "https://openalex.org/I1", "display_name": "Pfizer", "type": "company"}]}


def _jhu(auth):
    return at_institution(auth, JHU_OPENALEX_IDS, is_hopkins_affiliation, raw_strings="always")


def test_role_rank():
    assert ROLE_RANK == {"corresponding": 0, "last": 1, "first": 2}


def test_role_on_the_parser_fixtures():
    assert role(_pi(["I145311948"])) == "corresponding"
    assert role(_pi(["I145311948"], pos="middle")) == "middle"
    assert role(_pi(["I145311948"], pos="first")) == "first"
    assert role(COMPANY) == "middle"
    assert role(FILLER) == "middle"
    assert role(SHARED_FIRST) == "first"
    assert role({"author_position": "last", "is_corresponding": False}) == "last"
    assert role({}) == "middle"


def test_at_institution_always_on_the_parser_fixtures():
    assert _jhu(_pi(["I145311948"])) is True
    assert _jhu(_pi(["I999"])) is False
    assert _jhu(_pi([], raw=["Dept of Medicine, Johns Hopkins University School of Medicine"])) is True
    assert _jhu(_pi(["I999"], raw=["Dept of Medicine, Johns Hopkins University School of Medicine"])) is True
    assert _jhu(COMPANY) is False
    assert _jhu(FILLER) is False


def test_at_institution_only_when_no_institutions():
    raw = ["Dept of Medicine, Johns Hopkins University School of Medicine"]
    only = {"raw_strings": "only_when_no_institutions"}
    assert at_institution(_pi([], raw=raw), JHU_OPENALEX_IDS, is_hopkins_affiliation, **only) is True
    assert at_institution(_pi(["I999"], raw=raw), JHU_OPENALEX_IDS, is_hopkins_affiliation, **only) is False
    assert at_institution(_pi(["I145311948"]), JHU_OPENALEX_IDS, is_hopkins_affiliation, **only) is True


def test_is_individual():
    assert is_individual({"author": {"display_name": "Avery Quillfeather"}}) is True
    assert is_individual(FILLER) is True
    for name in ("Invented Trial Consortium", "Imaginary Research Group", "Made-Up Network",
                 "Fictional Study Investigators", "Hypothetical Cohort Study"):
        assert is_individual({"author": {"display_name": name}}) is False
    assert is_individual({"author": {"display_name": None}, "raw_author_name": "Pretend Consortium"}) is False
