"""R2.2 (author iDs), R2.6 (author details, C11), R2.7 (abstract), R2.8 (year) and R2.9
(one-line title and journal) of the 2026-10-15 publication-handling design."""
import pytest

from src.services import jhu_rules, pubmed


def _doc(medline_inner: str) -> str:
    return ('<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation>'
            f"<PMID>1</PMID>{medline_inner}</MedlineCitation></PubmedArticle></PubmedArticleSet>")


def _one(medline_inner: str) -> dict:
    (record,) = pubmed._parse_pubmed_xml(_doc(medline_inner))
    return record


def _affiliations(*texts: str) -> list[str]:
    infos = "".join(f"<AffiliationInfo><Affiliation>{t}</Affiliation></AffiliationInfo>" for t in texts)
    record = _one("<Article><ArticleTitle>T</ArticleTitle><AuthorList><Author><LastName>Doe</LastName>"
                  f"<ForeName>Jane</ForeName>{infos}</Author></AuthorList></Article>")
    return record["authors"][0]["affiliations"]


_AUTHOR_DETAILS = """<Article><ArticleTitle>T</ArticleTitle>
<AuthorList CompleteYN="N">
<Author ValidYN="Y"><LastName>Chen</LastName><ForeName>Ziheng</ForeName><Initials>Z</Initials>
<Identifier Source="ORCID">0000-0003-1918-106X</Identifier>
<AffiliationInfo><Affiliation>Department of Pathology, Houston, TX, USA. Electronic address: zc@x.org.</Affiliation></AffiliationInfo></Author>
<Author EqualContrib="Y"><LastName>Doe</LastName><ForeName>John</ForeName><Initials>J</Initials><Suffix>Jr</Suffix>
<AffiliationInfo><Affiliation>First Institute</Affiliation></AffiliationInfo>
<AffiliationInfo><Affiliation>Second Institute</Affiliation></AffiliationInfo></Author>
<Author ValidYN="N"><LastName>Wrong</LastName><ForeName>W</ForeName></Author>
<Author><CollectiveName>The X Consortium</CollectiveName></Author>
<Author><ForeName>NoLast</ForeName></Author>
</AuthorList></Article>"""


def test_author_details():
    """Trimmed from PMID 37069167."""
    record = _one(_AUTHOR_DETAILS)
    authors = record["authors"]
    assert record["author_count"] == 5
    assert [a["position"] for a in authors] == [1, 2, 3, 4, 5]
    assert authors[0]["orcid"] == "0000-0003-1918-106X" and authors[0]["fore"] == "Ziheng"
    assert "@" not in authors[0]["affiliations"][0]
    assert len(authors[1]["affiliations"]) == 2
    assert authors[1]["equal_contrib"] is True and authors[1]["suffix"] == "Jr"
    assert authors[1]["orcid"] is None
    assert authors[2]["valid"] is False
    assert authors[3]["collective"] == "The X Consortium"
    assert authors[4]["last"] is None
    assert record["author_list_complete"] is False


@pytest.mark.parametrize("author_list", [
    "", '<AuthorList CompleteYN="Y"><Author><LastName>A</LastName></Author></AuthorList>',
    "<AuthorList><Author><LastName>A</LastName></Author></AuthorList>",
])
def test_no_author_list(author_list):
    record = _one(f"<Article><ArticleTitle>T</ArticleTitle>{author_list}</Article>")
    assert record["author_list_complete"] is True
    if not author_list:
        assert record["authors"] == [] and record["author_count"] == 0


def test_affiliation_whitespace_is_collapsed():
    assert _affiliations("Dept   of X,\n JHU, Baltimore.") == ["Dept of X, JHU, Baltimore"]


@pytest.mark.parametrize("raw, expected", [
    ("Dept   of X,  JHU, Baltimore. Electronic address: jane@jhu.edu.", "Dept of X, JHU, Baltimore. jhu.edu"),
    ("Department of Medicine, Baltimore, MD 21205, USA. jdoe@jhmi.edu",
     "Department of Medicine, Baltimore, MD 21205, USA. jhmi.edu"),
    ("MD Anderson Cancer Center, Houston, TX, USA. a.b@mdanderson.org.",
     "MD Anderson Cancer Center, Houston, TX, USA. mdanderson.org"),
])
def test_affiliation_email_becomes_its_domain(raw, expected):
    (out,) = _affiliations(raw)
    assert out == expected
    assert "@" not in out and "jane" not in out and "jdoe" not in out and "a.b@" not in out


def test_email_domain_keeps_the_hopkins_tenure_evidence():
    (aff,) = _affiliations("Department of Medicine, Baltimore, MD 21205, USA. jdoe@jhmi.edu")
    assert jhu_rules.is_hopkins_affiliation(aff) is True
    assert jhu_rules.derive_start_from_papers(
        [{"year": 2005, "stages": ["s1"], "pi_affiliations": [aff]}]) == 2005


def test_double_space_hopkins_affiliation_now_matches():
    assert jhu_rules.is_hopkins_affiliation("Johns  Hopkins University") is False  # raw, today
    assert _affiliations("Johns  Hopkins University") == ["Johns Hopkins University"]
    assert jhu_rules.is_hopkins_affiliation("Johns Hopkins University") is True


def test_author_orcid_accepts_spaced_groups():
    record = _one('<Article><ArticleTitle>T</ArticleTitle><AuthorList><Author><LastName>Lee</LastName>'
                  '<Identifier Source="ORCID">0000 0002 1825 0097</Identifier></Author></AuthorList></Article>')
    assert record["authors"][0]["orcid"] == "0000-0002-1825-0097"


def test_other_abstract_is_not_concatenated():
    """Trimmed from PMID 26815162."""
    record = _one('<Article><ArticleTitle>T</ArticleTitle><Abstract>'
                  '<AbstractText Label="OBJECTIVE">To compare.</AbstractText>'
                  '<AbstractText Label="METHODS">We measured.</AbstractText></Abstract></Article>'
                  '<OtherAbstract Type="Publisher" Language="por">'
                  '<AbstractText Label="OBJETIVO">Comparar.</AbstractText></OtherAbstract>')
    assert "OBJETIVO" not in record["abstract"]
    assert record["abstract"].startswith("OBJECTIVE: ")


def test_english_other_abstract_is_a_fallback_only():
    eng = _one('<Article><ArticleTitle>T</ArticleTitle></Article>'
               '<OtherAbstract Language="eng"><AbstractText>English.</AbstractText></OtherAbstract>')
    assert eng["abstract"] == "English."
    por = _one('<Article><ArticleTitle>T</ArticleTitle></Article>'
               '<OtherAbstract Language="por"><AbstractText>Portugues.</AbstractText></OtherAbstract>')
    assert por["abstract"] == ""


@pytest.mark.parametrize("pubdate, article_date, expected", [
    ("<MedlineDate>Winter 1998-1999</MedlineDate>", None, 1998),
    ("<MedlineDate>Dec 1998-Jan 1999</MedlineDate>", None, 1998),
    ("<MedlineDate>2021 Sep-Oct 01</MedlineDate>", None, 2021),
    ("<Season>Spring</Season>", "2020", 2020),
    ("<Year>2023</Year>", "2022", 2023),
    ("<Year>n.d.</Year>", "2019", 2019),
    ("<MedlineDate>Spring</MedlineDate>", None, None),
])
def test_year_fallbacks(pubdate, article_date, expected):
    adate = (f'<ArticleDate DateType="Electronic"><Year>{article_date}</Year></ArticleDate>'
             if article_date else "")
    record = _one(f"<Article><Journal><JournalIssue><PubDate>{pubdate}</PubDate></JournalIssue>"
                  f"</Journal><ArticleTitle>T</ArticleTitle>{adate}</Article>")
    if expected is None:
        assert "year" not in record
    else:
        assert record["year"] == expected


def test_title_and_journal_are_one_line():
    record = _one("<Article><Journal><Title>J\r\nX</Title></Journal>"
                  "<ArticleTitle>A\nB\u2028C  D</ArticleTitle></Article>")
    assert record["title"] == "A B C D" and record["journal"] == "J X"
    assert _one("<Article><ArticleTitle></ArticleTitle></Article>")["title"] == ""
