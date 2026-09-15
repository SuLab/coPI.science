# Profile Ingestion Pipeline Specification

## Overview

The pipeline builds a ResearcherProfile from two sources: ORCID (profile, grants, publications) and PubMed (abstracts, optionally full-text methods). It produces structured profile fields via LLM synthesis. On first generation, it also seeds a private profile (agent instructions) for user review.

## Triggers

| Trigger | Behavior |
|---|---|
| User creates account with ORCID | Full pipeline (includes private profile seed) |
| User clicks "refresh profile" | Full pipeline |
| Monthly cron detects new ORCID works | Generate candidate profile, notify user if arrays changed |
| Admin seeds profile with ORCID ID | Full pipeline (no user interaction) |

## Pipeline Steps

### Step 1: Fetch ORCID Profile

```
GET https://pub.orcid.org/v3.0/{ORCID-ID}/record
Accept: application/json
```

Extract:
- Full name (given-names + family-name)
- Current affiliation (from employments)
- Lab website (from researcher-urls)
- Email (if public)

### Step 2: Fetch ORCID Grants

```
GET https://pub.orcid.org/v3.0/{ORCID-ID}/fundings
Accept: application/json
```

Extract grant titles from each funding entry. Store on profile as `grant_titles` array.

### Step 3: Fetch Publications from ORCID Works

```
GET https://pub.orcid.org/v3.0/{ORCID-ID}/works
Accept: application/json
```

The user has curated their publication list on ORCID. Pull the works list and extract PMIDs/DOIs for each entry. This eliminates author name disambiguation — if a paper is on their ORCID, they've claimed it.

**Fallback for sparse ORCID:** If the ORCID works list has fewer than 5 entries, nudge the user to update their ORCID profile rather than falling back to PubMed name search. Display: "We found [N] publications on your ORCID profile. For the best collaboration matching, please ensure your ORCID is up to date at orcid.org."

### Step 4: Fetch Abstracts from PubMed

Using PMIDs from the ORCID works list, batch fetch PubMed records:

```
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={PMID1},{PMID2},...&rettype=xml&retmode=xml
```

For each publication, extract and store as a Publication entity:
- PMID, PMCID (if available), DOI
- Title
- Abstract
- Journal
- Year
- Author position (first, last, middle) — determined from the author list in the PubMed record
- Article type (research article, review, editorial, etc.)

Store all publications. For profile synthesis, use the most recent 25-30 research articles (not reviews/editorials/commentaries), prioritizing last-author papers.

### Step 5: Deep Mining (Default On)

Fetch methods sections from PMC for open-access papers to extract specific techniques, cell lines, mouse strains, protocols, and reagents.

1. Identify which publications have PMCIDs (from Step 4 or via ID conversion):
   ```
   GET https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={PMID1},{PMID2},...&format=json
   ```

2. Fetch full text for open-access papers:
   ```
   GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={PMCID}&rettype=xml
   ```

3. Extract the methods/materials section from the XML.

4. Store as `methods_text` on the Publication entity.

Deep mining is on by default. It is slower (minutes vs seconds) but produces significantly more specific profiles, which directly improves matching engine output quality.

### Step 6: Seed Private Profile (First Generation Only)

On first profile generation (no existing `private_profile_md`), use the LLM to generate a draft private profile (agent instructions) based on the researcher's publications, grants, and research summary. The seed is stored in `private_profile_seed` on ResearcherProfile and presented to the user for review and editing during onboarding step 4. Once the user saves, the content is promoted to `private_profile_md` and written to `profiles/private/{agent_id}.md`.

This step is skipped on profile refreshes and re-generations — the user's existing private profile is preserved.

### Step 7: LLM Synthesis

Call the LLM (Claude Opus) with the assembled context to generate structured profile fields.

**Input to LLM:**

```
Researcher Information:
- Name: [name]
- Affiliation: [institution, department]
- Lab Website: [url, if available]

Grant Titles:
- [grant title 1]
- [grant title 2]
- ...

Publications (most recent 25-30 research articles, last-author prioritized):
- [title] ([journal], [year]) [last author / first author]
  Abstract: [abstract text]
- ...

Methods Sections (where available):
- From "[paper title]":
  [methods text excerpt]
- ...
```

**Prompt instructions for synthesis:**

- Generate a research summary of 150-250 words as a narrative that connects themes, not a list of topics
- Weight recent publications more heavily than older ones
- Be specific: "CRISPR-Cas9 screening in K562 cells" not just "CRISPR"
- For computational labs, list databases and computational resources as experimental models
- Extract specific molecular targets, not just pathways
- The profile must be justifiable from publicly available information

**Output schema:**

```json
{
  "research_summary": "150-250 word narrative",
  "techniques": ["technique1", "technique2", ...],
  "experimental_models": ["model1", "model2", ...],
  "disease_areas": ["area1", "area2", ...],
  "key_targets": ["target1", "target2", ...],
  "keywords": ["keyword1", "keyword2", ...]
}
```

**Field-specific guidance (carried over from Science Profiler skill):**

**Techniques:** Look for sequencing methods (RNA-seq, ChIP-seq, ATAC-seq, single-cell RNA sequencing), imaging (confocal microscopy, cryo-ET, live-cell imaging), biochemistry (mass spectrometry, proteomics, CRISPR screens), computational (machine learning, network analysis, knowledge graphs), molecular biology (gene editing, cloning, reporter assays), cell biology (flow cytometry, organoid culture), structural (X-ray crystallography, cryo-EM, NMR), in vivo (behavioral testing, metabolic phenotyping).

**Experimental Models:** Model organisms, specific cell lines (including variants), transgenic/knockout models with strain names, patient samples, and for computational labs: databases, knowledge graphs, text corpora.

**Disease Areas:** Use standardized terms. For basic science labs, list biological processes/systems rather than diseases.

**Key Targets:** Specific proteins, enzymes, receptors, transcription factors, pathways, gene families, molecular systems.

**Keywords:** Additional terms not already captured in other fields. Draw from MeSH terms. Optional — listing none is acceptable.

### Step 8: Validation

Before saving, check:
- Research summary is 150-250 words
- At least 3 techniques listed
- At least 1 disease area or biological process listed
- Key targets array is present (may be empty)

If validation fails: re-run synthesis once with stricter prompt. If it fails again, save what we have and flag for review.

### Step 9: Store

- Parse LLM output into structured fields
- Save to ResearcherProfile
- Bump `profile_version`
- Compute and store `raw_abstracts_hash` (hash of all abstract texts used as input)
- Set `profile_generated_at` to now

## Monthly Refresh Cron

1. For each user with an ORCID: re-fetch ORCID works list
2. Diff against stored Publication records
3. If new works found:
   a. Fetch abstracts for new publications
   b. Run deep mining for new publications
   c. Store new Publication entities
   d. Run LLM synthesis with updated publication set → candidate profile
   e. Compare candidate arrays against current arrays (techniques, experimental_models, disease_areas, key_targets, keywords, grant_titles)
   f. NOT IMPLEMENTED: this was designed to store a candidate as `pending_profile` for
      side-by-side review, but no code ever wrote that column (issue #22 V6-pend). Today
      a differing candidate is written straight to the live profile fields by whatever
      pipeline run produced it, gated only by `_validate_profile` (see PR V1/V6 fixes).
   g. If no arrays differ: no action needed (new publication stored but profile unchanged)
4. Refresh frequency is configurable (default: monthly). No regeneration if nothing changed.

## API Rate Limits

### ORCID API
- No authentication required for public data
- Rate limit: 24 requests/second, 10,000/day

### NCBI E-utilities (PubMed/PMC)
- Without API key: 3 requests/second
- With API key: 10 requests/second
- Get key at: https://www.ncbi.nlm.nih.gov/account/settings/
- The app should use an API key for all NCBI requests

## Edge Cases

### Computational vs Wet-Lab Profiles
For computational/bioinformatics labs:
- "Experimental models" includes databases, knowledge graphs, text corpora, claims data
- "Techniques" focuses on computational methods, algorithms, analysis approaches

### Sparse ORCID Data
If ORCID has minimal works:
- Nudge user to update ORCID
- Profile can still be generated from grants + any available publications
- Flag profile as potentially incomplete

### Publications Without PMIDs
Some ORCID works may only have DOIs. Use NCBI ID converter or CrossRef to resolve to PMIDs where possible. If no PMID available, store with DOI only and skip abstract fetch for that paper.
