# Help pages: how data is obtained and used — remediation plan

**Date:** 2026-10-15
**Baseline:** `blackbird` at `b3f5ce12` ("feat(help): Help tab, staff documentation and onboarding tutorials").
**Source of the findings:** a gap analysis of `templates/help/**` against the code, followed by two
adversarial audits (read-only, Opus) that tried to disprove each claim against the code at
`b3f5ce12`. In all, 40 items were checked:
- of 28 claims, 16 were confirmed, 10 partially confirmed and 2 refuted;
- the auditors found 12 more items: 11 help gaps and one UI defect.

**Goal:** after this change the Help tells staff — accurately, and inside its own content rules — how
publications, grants, industry evidence, companies and other data are obtained, which of them the AI
and the bots use, and what staff controls can and cannot change. One UI defect and two pieces of UI
copy that repeat the same misstatements are fixed with it.

**Out of scope:** any change to how the pipeline, the grant job, the industry scan, discovery or the
bots behave. Section 3 lists the product questions the findings raise; each is a separate change
if the owner wants it.

---

## 1. Rules every task follows

From `docs/operations/help-content.md` (content rules) and the root `CLAUDE.md`:

1. **Every fact comes from reading the current code.** The code evidence below was correct at
   `b3f5ce12`. Re-read each cited range before writing the sentence; if the code moved, cite and
   describe what is there now.
2. **Quote UI text exactly** (button labels, headings, badge words) as the page prints it.
3. **No numbers** except the 1–5 rating and the 10,000-character comment limit. Rules with a
   threshold are described without the number ("only a few papers", "the newest papers", "up to a
   limit"). This rules out stating the thin-record threshold, the 50/30 paper limits, the 20-paper
   persona list, the grant caps and the methods-section count.
4. **No repository internals:** no file paths, code names, test names or settings names in the help.
   The suggested wording below already obeys this. The code evidence column is for the implementer
   and the auditor only.
5. **Role-only text** on an all-audience page (`how-it-works`, `pis`, `glossary`, `assessments`)
   sits in `{% if permissions.staff %}…{% endif %}`. A reviewer reads a manager feature only as
   "a manager can…".
6. **Warn before real effects** with `{% call careful() %}`.
7. **A new section on a page** needs its anchor added to that page's `anchors` tuple in
   `src/web/help_pages.py` (`tests/unit/test_help_registry.py` checks declared anchors exist).
8. **A changed UI template** (task T9) makes screenshots stale. Capture is the last step before
   deploy, done on a workstation with Docker (task T11).
9. **The working tree is the production host's tree**, mounted over sshfs. Never `git checkout`,
   `stash`, `restore` or commit `docker-compose.prod.yml`. Commit only the files this plan lists.

---

## 2. Findings register

The type is one of:
- **M**: the help states something wrong;
- **O-H**, **O-M**, **O-L**: an omission of high, medium or low importance;
- **UI**: a defect in a page;
- **—**: refuted, so there is nothing to do.

The task column names the work package in section 4.

### 2.1 Misstatements (fix first)

| ID | Help says (location) | What the code does (evidence) | Task |
|---|---|---|---|
| A8 | "When no year has ever been set, Blackbird uses the PI's full career" (`managing-pis.html:193`), and "Publications from that year on ground the profile and the lab agent's publication list" (`:186-187`). The same claim appears at `glossary.html:215-218` ("JHU tenure start", "Full career"), `tutorials/manager/05-profile-review.html:39-42` and `pis.html:111-112` ("…not listed anywhere"). | The whole career is also used when only a few papers fall in the tenure window, even with a year set. Evidence: `tenure_scope.py:106-127`; profile and persona use it at `profile_pipeline.py:153-166, 685-694, 1119-1122` and `tenure_scope.py:418-419`. The PI page's list and count stay scoped to the window (`directory.py:400-402`, `tenure_scope.py:310-358`). | T2, T4, T5, T7, T8 |
| A11 | "otherwise it replaces the profile" (`managing-pis.html:332-334`) | If the new text fails the quality check twice, or was written from no abstracts while the stored profile had some, a stored profile that passed is kept and nothing changes. Evidence: `profile_pipeline.py:837-845, 897-907, 938-954`. Otherwise the text is saved with no page marker (`:985, 1024-1036`). | T2, T7 |
| A4 | "Papers found by the PI's name with no ORCID anchor" (`managing-pis.html:229`; `glossary.html:85-86`; `tutorials/manager/05-profile-review.html:76`), and the same in the **UI copy** at `templates/pis/_profile_review.html:54` | Every record with no ORCID anchor becomes a candidate, so papers found only by OpenAlex count too. Evidence: `corpus_additions.py:41-46, 307-312`; `profile_pipeline.py:596-597`. | T2, T5, T7, T9 |
| G2 | "Reject keeps it out" (`managing-pis.html:235`); "Reject is permanent" (`:240`, `glossary.html:85-88`) | A rejected candidate that later gains an ORCID anchor is stored anyway (`corpus_additions.py:307-310`). A pending candidate that gains one is stored and marked accepted (`:318-322`). | T2, T5, T7 |
| X1 | "Past grants are not shown when no JHU tenure start is recorded" (`pis.html:86-87`); the tenure-filter text at `pi-evidence.html:97-102` | Grants use the recorded year, or else the provisional one. Evidence: `jhu_rules.py:195-212`, used at `grant_enrichment.py:156` and `grant_sections.py:281`; the card at `pi_detail.html:360`. The industry score uses only a recorded or curated year (`industry_evidence.py:213-218`; `pi_detail.html:444`). | T3, T4, T7 |
| A14 | "Each PI has a lab agent" (`how-it-works.html:16`; `managing-pis.html:7-8`). This contradicts the "Not requested" status (`managing-pis.html:107-109`). | No agent row means `not_requested` (`directory.py:304-305`). An agent is created by Add PI (`pi_onboarding.py`) or when a PI requests one (`src/routers/agent_page.py`; verify the route). | T1, T2 |
| A7a | "whether its methods text was found ('Methods ✓' or 'No methods')" (`pis.html:109`) | Methods are fetched only for some of the newest offered papers (`profile_pipeline.py:730-754`). "No methods" usually means nobody looked. | T4, T9 (decision D3) |
| B10a | The lab agent "works from what Blackbird holds about the PI, chiefly their research profile" (`how-it-works.html:16-18`) | A lab agent's only record of its PI is its persona file (`agent.py:150-156, 322-323`). It also has its working memory (`agent.py:324-325`) and live PubMed look-ups (`pi_lab/role.toml:10`, `tools.py:184-216`). | T1 |

### 2.2 High-importance omissions

| ID | Missing | Code evidence | Task |
|---|---|---|---|
| A2 | The identity check and papers the help never shows. A paper is kept only when the PI appears on it by name as an individual author. Editorials, comments, letters, corrections, retraction notices, retracted papers and group-only papers are dropped. A paper whose authorship cannot be confirmed is dropped without being offered as a candidate, and no staff page shows it. Papers published under a former surname never match. | `corpus.py:80-94, 376-468, 577-655` (former surname `:401-408`); `corpus_additions.py:297-312`; only the job progress records them (`profile_pipeline.py:359-367`), and the Jobs table does not show progress (`pi_detail.html:679-719`) | T2 (decision D1) |
| A13 | Nothing refreshes a profile or its papers on a schedule. New publications appear only after a profile generation (Add PI, a PI's first sign-in or own refresh, Regenerate, Retry, a changed tenure start). | `worker/main.py:6, 285-288` (`monthly_refresh` retired). Generation is queued at `auth.py:238-241, 293-300`, `onboarding.py:83-91`, `profile.py:178-192`, `pi_onboarding.py:137, 210`, `profile_edit.py:148-160`, plus Regenerate and Retry. Persona sweep: `persona_sweep.py:1-9, 31-36` and `worker/main.py:500-513`; whether it is on is decision D4. | T1, T2 |
| G1 | Only papers on the Unanchored list can be excluded from the page. A wrong ORCID-confirmed paper, or a work stored without a PubMed record, has no Exclude button. | `_profile_review.html:76-85`; `profile_review.py:83-91`; the Publications list has no controls (`pi_detail.html:658-675`); `non_pubmed_works.py:14-17` | T2 (decision D2) |

### 2.3 Medium-importance omissions

| ID | Missing | Code evidence | Task |
|---|---|---|---|
| A1 | The four searches: the PI's ORCID works; OpenAlex works for their ORCID iD; PubMed papers carrying their ORCID iD; PubMed by name at the recorded institution (skipped with no institution). PubMed supplies the stored title, abstract, journal and year. OpenAlex is never named in the help. | `corpus.py:834-882, 884-911`; `corpus_additions.py:234-245` | T1, T2 |
| A5 | Works PubMed does not index (articles, conference papers, books, chapters, preprints from ORCID or OpenAlex) are stored without review for PIs with few PubMed papers in the window. An OpenAlex-only work must show a Hopkins or ORCID-listed affiliation, or the PI's ORCID iD in the publisher's metadata. These never appear on the review lists. | `profile_pipeline.py:350-353, 634-648`; `tenure_scope.py:130-135`; `non_pubmed_works.py:33-40, 137-149`; `profile_review.py:83-91` | T2 |
| A6 | Each generation refreshes stored papers from PubMed and never removes one, even if the searches stop finding it. Keep, Accept and Restore stop that refresh for the paper. The system excludes papers itself only as earlier versions or as copies of a stored paper. | `corpus_additions.py:8-9, 98-114, 149-173, 196-219, 273-286, 358-385`; `profile_review.py:153, 193, 242-243` | T2 |
| A7 | What the AI reads: the newest in-scope papers that have an abstract, part of each abstract, and methods sections from PubMed Central for some open-access papers. | `profile_pipeline.py:101-102, 153-166, 667-688, 730-754, 1176-1195` | T2 |
| A10 | The AI is also given the PI's name, institution, department and ORCID website, and the grant lines (ORCID fundings only on a first generation). | `profile_pipeline.py:777-782, 828-833, 1154-1174`; `prompts/profile-synthesis.md:3-4, 75-76` | T1, T2, T7 |
| G4 | The stored name and institution are what the next generation searches PubMed with and matches authors against. With no institution, the name search is skipped. ORCID fills these fields only where they are blank. | `profile_pipeline.py:270-280, 346-348`; `corpus.py:866-882` | T2 |
| G6 | The Careful box for Regenerate and Retry omits side effects. Every generation also re-reads ORCID fundings, refreshes grants and industry evidence, and reruns company discovery. | `profile_pipeline.py:298-326, 1058-1074` | T2, T7 |
| X2 | The Display Name is the search key for NIH RePORTER, conflict-of-interest attribution, OpenAlex, patents, trials and company discovery. A name edit alone queues no refresh. | `profile_edit.py:298-300`; `grant_enrichment.py:112-113, 168`; `industry_sources/registry.py:102, 114-120, 144, 164`; `company_discovery.py:593-594` | T2, T3 |
| B1 | Only research-project and career award types reach the persona. Training, instrument, conference, resource, centre and other types are listed on the card but never counted. | `grant_sections.py:157`; `grant_resolution.py:28-35, 148`; the card lists all: `grant_enrichment.py:225-237`, `pi_detail.html:386-410` | T3, T7 |
| B2 | When a grant counts as active or past, and that each list keeps only the most recent up to a limit. | `grant_sections.py:34-35, 116-131, 251-255` | T3, T5 |
| B3 | Every ORCID funding with a title enters the persona, whatever its funder, type or institution. That includes training and company funding, and pre-Hopkins funding that is still active. | `orcid_fundings.py:145-167`; `grant_sections.py:123-131, 185-203` | T3 |
| B5 | NIH awards are looked up after each generation and then added to the persona. A new PI's persona and research summary start with ORCID fundings only. Active or past can change with the date alone. | `profile_pipeline.py:5, 309-321, 1058-1074, 1154-1158`; `grant_enrichment.py:299-305`; `persona_sweep.py:3-8, 61-89` | T3 |
| B7 | Patents count only when they are Hopkins applications with the PI as first inventor and a title matching the profile's keywords, targets or disease areas. Trials count only when they are Hopkins-led with an industry collaborator and match the disease areas. Editing those lists changes the next scan, and a provisional tenure year is not used. | `industry_evidence.py:213, 221-222`; `uspto_inventor.py:133-151, 181-190`; `ctgov.py:61-72`; `jhu_rules.py:118-147` | T3 |
| B10 | What each AI sees: a lab agent its persona file, working memory and live paper look-ups; BlackbirdBot a lab's persona plus confirmed companies, only when it looks the lab up, for that reply; specialists only what BlackbirdBot passes them; the assessment chat no profile. | `agent.py:150-156, 300-306, 322-325, 433`; `tools.py:184-216, 398-432, 660-666`; `assessment_chat_record.py:74-86` | T1 |
| B13 | Working memory. Each lab agent and BlackbirdBot keep notes the AI rewrites after each thread closes and reads in every later turn. Notes carry over when a run is resumed but are archived by a Fresh run. BlackbirdBot's notes cover every lab, and Persona revisions does not show them. | `engine/memory.py:114-175, 136-149, 230-232, 251-258`; `agent.py:324-351`; `main.py:436-453`; `working_memory_reset.py:1-10`; `revision_history.py:29-35` | T1 |
| X3 | During an interview BlackbirdBot can look papers up in PubMed and search US patent filings for prior art; lab agents can look papers up too. | `pi_lab/role.toml:10`; `scout_hub/role.toml:11`; `tools.py:184-216, 566-583`; `pubmed.py:1152, 1211` | T1 |

### 2.4 Low-importance omissions

| ID | Missing | Code evidence | Task |
|---|---|---|---|
| A3 | Copies of one paper (same title, or a preprint and its published version) merge into one, keeping the published version. A stored earlier version moves to Excluded papers. | `corpus.py:668-736`; `corpus_additions.py:149-173`; `non_pubmed_works.py:266-280` | T2 |
| A9 | The persona lists the PI's newest in-scope papers by title only, each with a DOI or PubMed link. The lab agent gets no abstracts. | `profile_export.py:101-121, 131-148` | T2, T5 |
| A12 | Tenure-year precedence. A recorded year always wins and is never re-derived. Without one the order is: the older curated list, then a current Hopkins position on ORCID, then the earliest ORCID-confirmed paper on which the PI's own affiliation is Hopkins. A year from papers is provisional when ORCID could not be read or the search was incomplete. | `profile_pipeline.py:506-563`; `jhu_rules.py:97-147` | T2, T7 |
| B4 | An ORCID funding that matches a listed NIH award by title or project number, and repeated items within each source, appear once, so the persona's counts can be lower than the card's lists. | `grant_sections.py:242-249` | T3 |
| B8 | An AI model reads the conflict-of-interest statements that mention founding a company, and picks out the company and the PI's role. Discovery reads all the PI's papers, not only those within tenure. | `company_discovery.py:3-6, 218-227, 245-247, 299` | T3, T5 |
| B9 | Confirmed companies (role, funding, sources) are shown only to BlackbirdBot when it looks the PI up, and on the assessment page. Lab agents and the assessment chat never see them. | `tools.py:370-377, 424-432`; `assessment_chat_record.py:326-352`; `pi_companies.py:572-589` | T3 |
| B11 | The words in Techniques, Model Systems, Disease Areas, Key Molecular Targets and Keywords decide which topic channels a lab agent joins, so editing them can change where it posts. | `engine/post_lane.py:35-44`; `persona_sections.py:16-22, 40-50`; `engine/constants.py:71-95` | T1, T2 |
| B12 | Papers in the persona count as the lab's own work in conversations. | `agent.py:196-212, 408-416`; `tools.py:186-201` | T1 |
| G3 | The "stages" on a candidate row (for example `s2,s4`) name the searches that found it. | `_profile_review.html:61` | T2, T5 (decision D5) |
| G5 | While the tenure year is provisional, the profile and persona use it, but the PI page's list and Pubs (tenure) count show the whole career. | `tenure_scope.py:257-307, 421-427`; `directory.py:400-402`; `profile_pipeline.py:553, 576`; `pi_detail.html:642` | T2, T4 |
| X4 | Discovery finds only companies the PI founded or co-founded. Advisory and board roles are added by hand. | `coi_founders.py:53`; `company_discovery.py:3-6` | T3, T5 |
| X6 | An AI model writes the chat's opening questions from the same record the chat reads, with a fixed fallback. | `assessment_chat_suggestions.py:1-13, 23-25` | T6 |

### 2.5 UI defect

| ID | Defect | Evidence | Task |
|---|---|---|---|
| X5 | The Grants card's "Tenure filter:" line prints "organisation and fiscal year ≥ tenure start" when no awards are stored. It reads the mode from the first award row, which can be an old vetoed one, so it can be wrong. | `templates/workspace/pi_detail.html:387-388` | T9 |

### 2.6 Refuted (no action)

| ID | Why |
|---|---|
| B6 | The RePORTER match rule in the help matches the code at the level it describes (`grant_enrichment.py:112-113, 182-185`; `grant_resolution.py:116-133`). |
| B14 | The assessment chat's sources are listed correctly at `tutorials/reviewer/06-assessment-chat.html:16-19` (`assessment_chat_record.py:74-86, 783-794`). Optional: repeat the list at `assessments.html` near line 190 (task T6). |

Also checked and correct: the industry score never enters the persona, and BlackbirdBot and the chat
never see it (`pi-evidence.html:143-145`).

---

## 3. Decisions for the owner (defaults apply if not answered)

| # | Question | Default in this plan |
|---|---|---|
| D1 | Should staff be able to see papers the identity check left out (A2)? | **Document only.** A view of left-out papers is a separate product change. |
| D2 | Should staff be able to exclude any stored paper, not just unanchored ones (G1)? | **Document only**, with the help saying which papers cannot be excluded from the page and what to do. A wider Exclude is a separate product change. |
| D3 | The "No methods" marker on the PI page's publication list (A7a). | **Relabel it "No methods fetched"** in `pi_detail.html:664` (task T9), and explain both markers in the help. |
| D4 | Is the daily persona sweep switched on in production? | **Wording that does not depend on it:** the help says no profile refresh is scheduled, and that grant lines follow the date when the persona is next rewritten. |
| D5 | Content rule 3 allows no numbers. Do the UI's own stage labels `s1`–`s4` count? | **Allowed**, because rule 2 requires naming UI text exactly. They are explained in the glossary. |
| D6 | Should the candidate-paper UI copy change with the help (A4)? | **Yes** (task T9). |

---

## 4. Tasks (disjoint file ownership)

T1–T8 edit only Jinja help content and can run in parallel. T9 changes UI templates and adds a
test. T10 owns the registry. T11 (screenshots) runs last, after review. All wording below is a
suggestion: re-check it against the code (rule 1) and the page's tone, and keep UI labels exact.

### T1 — `templates/help/pages/how-it-works.html` (audience: all)

**Findings:** A14, B10a, A1 (overview), A10 (overview), A13 (overview), B10, B11, B12, B13, X3.

1. "PIs and lab agents":
   - Replace "Each PI has a lab agent" with: a PI's lab agent is created when a manager adds the PI with Add PI, or when the PI requests one. Until then the PI has none. Verify the request route in `src/routers/agent_page.py` first.
   - Replace "works from … chiefly their research profile" with: the lab agent knows its PI only through its persona, a text built from the research profile, the newest papers and the grant lines.
2. New section **`data-sources` — "Where the data comes from"**, a short overview with links to the
   staff pages for detail:
   - publications come from the PI's ORCID record, OpenAlex and PubMed, and PubMed supplies each paper's stored title and abstract;
   - grants come from ORCID and NIH RePORTER;
   - industry evidence comes from public paper, patent and trial records;
   - companies are confirmed by staff;
   - the profile is written by an AI model from the papers, the grant lines and the PI's name, institution, department and ORCID website;
   - nothing refreshes a profile on a schedule: new papers arrive only with a new profile generation.

   Put the staff-page links inside `{% if permissions.staff %}`.
3. New section **`what-agents-see` — "What each AI agent sees"**:
   - **Lab agent:** its persona, its working memory, and papers it looks up in PubMed during a thread. The words in the profile's lists choose its Slack topic channels. Papers in its persona count as its own work.
   - **BlackbirdBot:** a lab's persona, with any staff-confirmed companies, only when it looks the lab up during an interview, and only for that reply. It also has its working memory, PubMed look-ups and a search of US patent filings for prior art.
   - **Specialists:** only what BlackbirdBot writes to them.
   - **Assessment chat:** the assessment's record, never the profile.
   - **Working memory:** notes the AI rewrites after each thread closes. They carry over when a run is resumed and are set aside when a fresh run starts. BlackbirdBot's notes cover every lab it has interviewed.
4. "Interviews": add the look-ups (X3) in one sentence, or link to `what-agents-see`.

**Acceptance:**
- the page no longer says every PI has a lab agent;
- both new sections exist with ids `data-sources` and `what-agents-see`;
- every statement traces to the evidence in section 2;
- no numbers, no file or setting names.

### T2 — `templates/help/pages/managing-pis.html` (audience: staff)

**Findings:** A1, A2, A3, A4, A5, A6, A7, A8, A9, A10, A11, A12, A13, A14, G1, G2, G3, G4, G5, G6,
X2, B11.

1. Intro (`:7-8`): drop "Each PI … gets … a lab agent". Say Add PI creates one.
2. New section **`finding-papers` — "How papers are found and kept"**, placed before "Reviewing papers":
   - **The four searches (A1):** the PI's ORCID record, OpenAlex's works for their ORCID iD, PubMed papers carrying their ORCID iD, and PubMed by name at the PI's recorded institution. The name search is skipped without an institution. PubMed supplies the stored title, abstract, journal and year.
   - **Anchored papers:** a paper the PI's ORCID record or PubMed's ORCID field confirms is stored without review. The others become candidate papers.
   - **The identity check (A2):**
     - the PI must appear on the paper by name as an individual author;
     - editorials, comments, letters, corrections, retraction notices, retracted papers and papers credited only to a group are left out;
     - a paper whose authorship cannot be confirmed is left out and is not offered as a candidate, and no page shows it;
     - papers under a former surname never match.

     Add the practical advice: a paper missing from every list usually failed this check, so check the name, institution and ORCID record.
   - **Duplicates (A3):** copies merge, and the published version is kept. An earlier version already stored moves to Excluded papers.
   - **Works PubMed does not index (A5):** for a PI with only a few PubMed papers in the tenure window, these are stored without review. An OpenAlex-only work needs a Hopkins or ORCID-listed affiliation, or the PI's ORCID iD. These never appear on the review lists and cannot be excluded from the page.
   - **Refresh (A6, A13):** each generation refreshes stored papers from PubMed and never removes one. Keep, Accept and Restore stop the refresh for that paper. Nothing refreshes on a schedule: new papers appear only after a generation (list what starts one).
3. New section **`writing-profile` — "What the profile is written from"** (A7, A10, A11):
   - the newest papers in scope that have an abstract, part of each abstract, and methods sections from PubMed Central for some open-access papers;
   - the grant lines (on a first generation, ORCID fundings only);
   - the PI's name, institution, department and ORCID website.
   - Quality checks: the summary's length, enough techniques, a disease area. A failed check is retried once. If it fails again, it never replaces a stored profile that passed (the generation ends with nothing changed); otherwise it is saved with no mark on the page.
4. "Editing a profile":
   - **Display Name (X2, G4):** the stored name and institution are what the next generation searches with, and the name is also the search key for grants, industry evidence and company discovery. A change takes effect at the next generation.
   - **Lists (B11):** editing the lists can change the lab agent's topic channels and the patents and trials the next industry scan counts.
5. "JHU tenure start" (A8, A12, G5):
   - Replace "When no year has ever been set, Blackbird uses the PI's full career" with both cases: no year recorded, or only a few papers in the window. In either case the profile and persona use the whole career, while the PI page's list and count stay on the window.
   - Add the precedence (A12). A recorded year always wins.
   - Add that a provisional year scopes the profile, persona and grants but not the PI page's list or the industry score (G5, X1).
6. "Reviewing papers":
   - **Candidate papers (A4):** "found only by OpenAlex or by the name search, so nothing ties them to the PI's ORCID iD".
   - Explain the **stages** on a candidate row (G3, decision D5).
   - **Reject (G2):** reword to "Reject stops the paper being offered again; if the PI's ORCID record or PubMed later ties it to the PI, the next generation stores it anyway". Edit the Careful box (`:240`) to match.
   - **Unanchored papers (G1):** add that only papers on this list can be excluded from the page. A wrong paper that the PI's ORCID record confirms, or a work stored without a PubMed record, cannot be excluded here. Name the route that exists: only say "only an admin can…" if an admin path exists in the code; otherwise say that the page cannot exclude it.
7. "Regenerate and Retry" (A11, G6):
   - Fix "otherwise it replaces the profile" (`:332-334`).
   - Extend the Careful box: every generation also re-reads ORCID fundings, refreshes grants and industry evidence, and reruns company discovery.
8. "The persona file" (A9): the persona lists the newest papers in scope by title, each with a DOI or PubMed link, and gives the lab agent no abstracts. Link to `how-it-works#what-agents-see`.

**Acceptance:**
- all listed findings addressed, with the new sections `finding-papers` and `writing-profile`;
- the Careful boxes updated;
- no numbers.

### T3 — `templates/help/pages/pi-evidence.html` (audience: staff)

**Findings:** B1, B2, B3, B4, B5, B7, B8, B9, X1, X2, X4.

1. Intro: the PI's stored name is the search key for these cards (X2), and a change applies at the next generation.
2. "The RePORTER identity" (B5): the NIH lookup runs after each profile generation. A new PI's persona and first research summary carry ORCID fundings only.
3. New section **`grant-lines` — "Which grants the persona lists"**, after the awards and fundings:
   - **Award types (B1):** only research-project and career award types reach the persona. Training, instrument, conference, resource, centre and other types are listed on the card but not counted.
   - **Active and past (B2):** a grant is active until its end date. An NIH award with no end date stays active while its latest funded year is current, and an ORCID funding with no end date stays active for some years after it starts. Ended grants within tenure are past. Each list keeps the most recent grants, up to a limit.
   - **Duplicates (B4):** an ORCID funding matching a listed NIH award is shown once, so the persona's counts can be lower than the lists.
   - **Dates (B5):** which grants are active changes with the date, so the persona's grant lines change when it is next rewritten.
4. "NIH RePORTER awards" (X1): the tenure filter and past grants use a provisional year when no year is recorded. The "No JHU tenure start recorded" note appears only when there is neither.
5. "ORCID fundings" (B3): every ORCID funding with a title is used whatever its funder, type or institution, including fellowships, training and company funding, and funding from before Hopkins that is still active. Veto any that should not describe the lab.
6. "Industry interest" (B7, X1):
   - name trials alongside patents in the source list;
   - patents count only as Hopkins applications naming the PI as first inventor, with a title matching the profile's lists;
   - trials count only when Hopkins-led, with an industry collaborator, matching the disease areas;
   - editing those lists changes the next scan;
   - a provisional tenure year is not used.
7. "Companies" (B8, B9, X4):
   - discovery finds only founder and co-founder ties; add advisory and board roles by hand;
   - an AI model reads conflict-of-interest statements that mention founding, so check the quoted statement before confirming;
   - confirmed companies reach only BlackbirdBot (when it looks the PI up) and the assessment page; lab agents and the assessment chat never see them.

**Acceptance:**
- all listed findings addressed;
- new section `grant-lines`;
- no numbers (no activity codes, caps or years).

### T4 — `templates/help/pages/pis.html` (audience: all)

**Findings:** A7a, A8, G5, X1.

1. "Grants" (`:86-87`): past grants need a tenure start, recorded or provisional (X1).
2. "Publications" (`:107-112`):
   - **Markers (A7a, decision D3):** "Methods ✓" means a methods section was fetched for that paper; "No methods fetched" means none was, usually because only some of the newest open-access papers are looked up.
   - **Window (A8, G5):** replace "not listed anywhere" with: this list shows only the tenure window, while the profile and persona use the whole career when only a few papers fall in it, or while there is only a provisional year.

**Acceptance:**
- the marker text matches T9's label exactly;
- no reviewer-visible staff instructions.

### T5 — `templates/help/pages/glossary.html` (audience: all)

**Findings:** A4, A8, A9, B2, B8, G2, G3, X4, plus new terms.

- **"Candidate papers" (staff):** A4 and G2 wording, the same as T2.
- **"Full career" and "JHU tenure start":** add the few-papers case and the provisional-year case (A8, G5).
- **New "Active and past grants"** (`term-active-past-grants`): B2 wording, without numbers.
- **New "Methods ✓, No methods fetched"** (`term-methods`): T4 wording.
- **New "Stages" (staff, `term-stages`):** what `s1`–`s4` mean (decision D5).
- **"Persona" (staff):** what it holds (A9): the profile text, the newest papers by title with links, and the grant lines.
- **"Discovery" (staff):** an AI model reads conflict-of-interest statements; only founder and co-founder ties (B8, X4).
- **New "Anchored paper" (staff, `term-anchored`):** confirmed by the PI's ORCID record or PubMed's ORCID field.

Keep alphabetical order and the `term-…` id pattern. Do not add terms the pages never show (rule 2), so there are no entries for "flagged" or "working memory".

**Acceptance:**
- the terms exist with stable ids;
- staff-only terms are in `{% if permissions.staff %}`;
- the wording is consistent with T2 to T4.

### T6 — `templates/help/pages/assessments.html` (audience: all)

**Findings:** X6, B14 (optional).

1. Opening questions (`~:205-206`): an AI model writes them from the same record the chat reads.
2. Optional: near `:190`, repeat the list of what the chat reads from the reviewer tutorial (verdict, transcript, panel findings, reviews, rubric).

**Acceptance:** `tests/unit/test_help_stated_constants.py` still passes, since this file holds the pinned constants. Do not touch the `reviewing` section.

### T7 — manager tutorial steps: `templates/help/tutorials/manager/04-pi-lifecycle.html`, `05-profile-review.html`, `06-evidence.html` (audience: staff)

**Findings:** A4, A8, A10, A11, A12, A13, B1, G1, G2, G6, X1.

- **04 (`:28`, `:32-37`):** the profile is written from the papers, grant lines and the PI's details (A10). Nothing refreshes on a schedule (A13). Add the side effects of Retry (G6).
- **05:**
  - `:39-42` tenure: A8 and A12 in brief.
  - `:58-61` regenerate: A11 and G6.
  - `:76` candidates: A4.
  - Reject: G2.
  - Unanchored and Exclude: G1.
  - Link each topic to the T2 sections.
- **06:** award types and grant-line rules in one sentence each, with a link to `pi-evidence#grant-lines` (B1). Provisional year for grants (X1).

**Acceptance:** each step stays short (one screen); details link to the reference sections.

### T8 — reviewer tutorial: `templates/help/tutorials/reviewer/08-pi-pages-and-help.html` (audience: all)

**Findings:** A8, X1.

- **`:53-54` Grants:** past grants need a recorded or provisional tenure start.
- **`:63-64` Publications:** the list shows the tenure window; the profile can draw on the whole career when the window holds only a few papers.

**Acceptance:** no staff instructions; the wording matches T4.

### T9 — UI templates and a regression test

**Files:**
- `templates/pis/_profile_review.html`;
- `templates/workspace/pi_detail.html`;
- the route or view that builds the PI-detail context, only if X5 needs a new context value (find it with `lathe:scout`);
- a new test, `tests/integration/test_pi_detail_grant_tenure_line.py`.

**Findings:** A4 (UI copy), G2 (UI copy), A7a (decision D3), X5.

1. **`_profile_review.html:54`** (A4, G2): "Found only by OpenAlex or the name search, with no ORCID anchor. Accept stores the paper; a rejected paper is not offered again, but the next generation stores it if the PI's ORCID record later confirms it." Keep T2 and T5 quoting this exactly.
2. **`pi_detail.html:664`** (A7a, D3): "No methods" becomes "No methods fetched".
3. **`pi_detail.html:387-388`** (X5): derive the tenure-filter line from the PI's effective tenure year (recorded, else provisional), as the grant job does (`jhu_rules.py:195-212`), not from the first award row. With no year at all, print the organisation-only text.
4. **Regression test** covering three cases:
   - no awards stored and no tenure year;
   - no awards and a provisional year;
   - a first award row that is vetoed and carries an outdated mode.

**Acceptance:**
- the new test passes;
- `tests/integration/test_help_wording.py`, `tests/integration/test_rendered_page_gate.py` and `tests/integration/test_workspace_contract.py` pass;
- `templates/help/**` quotes the new UI strings exactly.

### T10 — registry: `src/web/help_pages.py`

Add the anchors that T1–T3 create:
- `how-it-works`: `data-sources`, `what-agents-see`;
- `managing-pis`: `finding-papers`, `writing-profile`;
- `pi-evidence`: `grant-lines`.

No other change.

**Acceptance:** `tests/unit/test_help_registry.py` passes, and every new anchor exists in its template.

### T11 — screenshots (sequential, last, on a workstation)

**Files:** `help_assets/shots/*.png`, `help_assets/shots/manifest.json`.

- After T1–T10 are merged, reviewed and fixed, run `python -m tests.e2e.help_shots.capture`. It needs Docker and a Chromium headless shell, and the host cannot run it.
- `pi-grants`, `pi-paper-review` and `pi-detail-reviewer` change with T9. A template change elsewhere marks other shots stale, so capture everything that `--check` lists.
- Commit the PNGs and the manifest together.

**Acceptance:**
- `python -m tests.e2e.help_shots.capture --check` exits 0;
- `tests/unit/test_help_shots.py` passes;
- the total PNG size stays under its test limit.

### T12 — operations doc: `docs/operations/help-content.md`

Add a row to "When a page changes, update its help" for `templates/pis/_profile_review.html`. It is
part of the PI page but is not listed today, which is how its wording drifted together with the help's.

---

## 5. Verification (after T1–T10 are merged, before T11)

Run on the host (pytest over sshfs is very slow):

1. **Help tests:**
   `.venv-test/bin/python -m pytest tests/unit/test_help_registry.py tests/unit/test_help_stated_constants.py tests/unit/test_reachability.py tests/unit/test_no_copi_in_templates.py tests/integration/test_help_routes.py tests/integration/test_help_links_render.py tests/integration/test_help_wording.py tests/integration/test_help_copy.py tests/integration/test_rendered_page_gate.py tests/integration/test_workspace_contract.py tests/integration/test_pi_detail_grant_tenure_line.py`.
   The link test renders every help page and step as reviewer, manager and admin, with Microsoft sign-in off and on.
2. **Number scan:** grep `templates/help/**` for digits outside Jinja, ids and classes. The only hits allowed are 1–5, 10,000 and the `s1`–`s4` stage labels (decision D5).
3. **Internals scan:** grep `templates/help/**` for `.py`, `src/`, `prompts/` and setting names. There should be no hits.
4. **Full gate:** `./scripts/ci.sh` (CLAUDE.md: alembic sanity, CSS drift, migration round trip, ruff, full pytest with the coverage floor). Since T9 touches no CSS class, `scripts/build_css.sh --check` should show no drift.

## 6. Audit (after a green pass)

Dispatch in one batch:
- `lathe:plan-auditor` with this plan and the full diff, proving every finding ID in section 2 is addressed by the stated task and acceptance criterion;
- `lathe:semantic-reviewer` on T9's diff;
- `lathe:auditor` (Opus) to re-verify every new or changed help sentence against the code at the new HEAD. This is content rule 1: each sentence gets confirmed, wrong, or imprecise.

Fix the confirmed findings, rerun section 5, and re-audit only the fixed sentences. Then run T11.

## 7. Commit and deploy (operator-gated)

- **Commit** only the files in T1–T12, after `ci.sh` passes:
  `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini help_assets`
  must show nothing untracked from this change. The builder's `git clean` drops untracked files, and `help_assets` is in the pre-build path list. Do not touch `docker-compose.prod.yml`. Do not push `blackbird` blind (CLAUDE.md, public `origin`).
- **Deploy** is app-only, with no migration and no worker or agent change:
  1. `DC="docker compose -f docker-compose.prod.yml"`.
  2. `$DC build blackbird-app`.
  3. `$DC up -d blackbird-app`.

  Never `--build` with `up`, never `--remove-orphans`, never restart the agent. Tag a rollback first (the project uses `rollback-pre-<change>` tags).
- **Smoke check after deploy:**
  - as a manager, open `/help/managing-pis#finding-papers`, `/help/pi-evidence#grant-lines` and a PI page with no awards (the Tenure filter line);
  - as a reviewer, open `/help/how-it-works#what-agents-see` and confirm no staff links are shown.

## 8. Related items found on the way, not in this plan

- `scripts/wipe_slack.py --memory` deletes only old-style `profiles/memory/<agent ID>.md` files. Current memories live in `profiles/memory/<agent ID>/public.md`, so the option no longer resets them; a Fresh run does. Fix it or remove the option in a separate change.
- Decisions D1 and D2 (a view of left-out papers; Exclude for any stored paper) are product changes that need their own design.

## 9. Done when

- every finding in section 2 except B6 and B14 is addressed in the task it is assigned to;
- verification passes;
- the audit's confirmed findings are fixed;
- screenshots are fresh;
- the change is committed (and deployed, if the owner asks).
