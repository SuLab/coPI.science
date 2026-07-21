"""Tests for the DOI normalization + PMID validation gate (pubmed.py).

Guards the fix for the bad-paper-link incident (GitHub issue #5): a stored DOI
must never disagree with the DOI registered for its PMID.
"""


from src.services.pubmed import _parse_pubmed_xml, normalize_doi, reconcile_pub_doi

# A PubMed record whose <ReferenceList> cites a paper with its own DOI/PMCID.
# The article's real DOI is 10.1126/scitranslmed.adn2601; the cited reference
# carries 10.5281/zenodo.15190299. The old parser grabbed the reference's.
_XML_WITH_REFERENCES = """<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>40802741</PMID>
   <Article>
    <ArticleTitle>Engraftment and persistence of gene-edited cells</ArticleTitle>
    <Journal><Title>Science translational medicine</Title></Journal>
   </Article>
  </MedlineCitation>
  <PubmedData>
   <ArticleIdList>
    <ArticleId IdType="pubmed">40802741</ArticleId>
    <ArticleId IdType="pmc">PMC12490786</ArticleId>
    <ArticleId IdType="doi">10.1126/scitranslmed.adn2601</ArticleId>
   </ArticleIdList>
   <ReferenceList>
    <Reference>
     <ArticleIdList>
      <ArticleId IdType="pmc">PMC8571176</ArticleId>
      <ArticleId IdType="doi">10.5281/zenodo.15190299</ArticleId>
     </ArticleIdList>
    </Reference>
   </ReferenceList>
  </PubmedData>
 </PubmedArticle>
</PubmedArticleSet>"""

# A record with no ArticleIdList DOI but an article-scoped ELocationID DOI.
_XML_ELOCATION_ONLY = """<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>12345678</PMID>
   <Article>
    <ArticleTitle>A paper</ArticleTitle>
    <ELocationID EIdType="doi">10.1000/elocation-doi</ELocationID>
   </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
    <ArticleId IdType="pubmed">12345678</ArticleId>
  </ArticleIdList></PubmedData>
 </PubmedArticle>
</PubmedArticleSet>"""


class TestParsePubmedXmlIds:
    def test_ignores_reference_list_dois(self):
        """The article's own DOI/PMCID is used, never a cited reference's."""
        rec = _parse_pubmed_xml(_XML_WITH_REFERENCES)[0]
        assert rec["doi"] == "10.1126/scitranslmed.adn2601"
        assert rec["pmcid"] == "PMC12490786"
        # The reference's identifiers must not leak in.
        assert rec["doi"] != "10.5281/zenodo.15190299"
        assert rec["pmcid"] != "PMC8571176"

    def test_elocation_id_doi_fallback(self):
        rec = _parse_pubmed_xml(_XML_ELOCATION_ONLY)[0]
        assert rec["doi"] == "10.1000/elocation-doi"


class TestNormalizeDoi:
    def test_none_and_empty(self):
        assert normalize_doi(None) is None
        assert normalize_doi("") is None
        assert normalize_doi("   ") is None

    def test_plain_doi_unchanged(self):
        assert normalize_doi("10.1038/s41586-020-2649-2") == "10.1038/s41586-020-2649-2"

    def test_strips_doi_prefix(self):
        # Real case: cochran's stored "doi:10.1038/s41467-017-00866-0"
        assert normalize_doi("doi:10.1038/s41467-017-00866-0") == "10.1038/s41467-017-00866-0"
        assert normalize_doi("DOI: 10.1038/x") == "10.1038/x"

    def test_strips_url_prefix(self):
        assert normalize_doi("https://doi.org/10.1038/x") == "10.1038/x"
        assert normalize_doi("http://dx.doi.org/10.1038/x") == "10.1038/x"

    def test_strips_trailing_junk(self):
        # Real case: liu's stored "10.1037/0033-2909.87.2.245."
        assert normalize_doi("10.1037/0033-2909.87.2.245.") == "10.1037/0033-2909.87.2.245"
        assert normalize_doi("  10.1038/x  ") == "10.1038/x"

    def test_preserves_case(self):
        # DOIs are case-insensitive but the registered form carries case.
        assert normalize_doi("10.1042/BJ20141349") == "10.1042/BJ20141349"


class TestReconcilePubDoi:
    def test_none_none(self):
        assert reconcile_pub_doi(None, None) == (None, "none")

    def test_no_authoritative_keeps_assigned(self):
        # PMID has no DOI on file -> can't validate, keep what we have (normalized).
        assert reconcile_pub_doi("doi:10.1/x", None) == ("10.1/x", "unverified")

    def test_fills_missing_doi(self):
        assert reconcile_pub_doi(None, "10.1/x") == ("10.1/x", "filled")

    def test_match_returns_canonical(self):
        assert reconcile_pub_doi("10.1/x", "10.1/x") == ("10.1/x", "ok")

    def test_match_is_case_insensitive_keeps_stored_form(self):
        # Match by case-insensitive compare; keep the stored form to avoid
        # churn (and esummary's lowercasing). Both resolve identically.
        assert reconcile_pub_doi("10.1042/BJ20141349", "10.1042/bj20141349") == (
            "10.1042/BJ20141349", "ok",
        )

    def test_match_canonicalizes_format_drift(self):
        # Stored carries a doi: prefix but is otherwise the right DOI.
        assert reconcile_pub_doi("doi:10.1/x", "10.1/x") == ("10.1/x", "ok")

    def test_mismatch_is_corrected_to_authoritative(self):
        # The core guard: a wrong link is replaced by the PMID's real DOI.
        assert reconcile_pub_doi("10.1038/nprot.2008.211", "10.1039/d3cb00044c") == (
            "10.1039/d3cb00044c", "corrected",
        )

    def test_real_slash_vs_dot_corruption(self):
        # corey: stored "10.1038/sj/onc/1205063" vs authoritative "10.1038/sj.onc.1205063".
        final, action = reconcile_pub_doi("10.1038/sj/onc/1205063", "10.1038/sj.onc.1205063")
        assert final == "10.1038/sj.onc.1205063"
        assert action == "corrected"

    def test_never_keeps_disagreeing_doi(self):
        # Whatever happens, the returned DOI is never the disagreeing assigned one.
        final, action = reconcile_pub_doi("10.1/wrong", "10.2/right")
        assert final == "10.2/right"
