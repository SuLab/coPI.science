# Scripps Research landscaping test run — implementation plan

> **For agentic workers:** Phase A (Tasks 1–4) runs through `/lathe:plan-execution` as three
> file-disjoint packages: **A** = Task 1 then Task 2 (they share `intake.py`, `test_qualify.py`
> and `test_intake.py`), **B** = Task 3, **C** = Task 4. Implementers do not build or test; that
> override of the per-task test steps below is deliberate, and Task 5 verifies everything once
> after the merge; the generator in Task 3 Step 4 is not a test and is run. Task 0 runs first,
> in the main session, once this plan and the spec are committed. Phase B (Tasks 6–10) is the run
> itself: operational, in order, in the main session, with two owner gates. Steps use checkbox
> (`- [ ]`) syntax.

**Goal:** Point the Hopkins landscaping pipeline at Scripps Research (La Jolla) on the
`scripps-landscaping` branch, run it once on the workstation, and hand the owner a database dump
the website can serve to two reviewers.

**Architecture:** Hard-code Scripps on the branch (spec C1): the institution constants, one new
per-address OpenAlex rule, the source queries, and the words the model and the reviewers read.
The pilot fetches the whole one-year window (C2). A separate compose project on the workstation
runs Postgres, the web app and the landscape worker; a small operator script replaces the admin
web actions.

**Tech Stack:** Python 3.11 image (tests on Python 3.12), FastAPI, SQLAlchemy async, Postgres 15,
pytest with testcontainers, Docker Compose 5.6 on the workstation.

**Spec:** `docs/specs/2026-11-20-scripps-landscaping-design.md`, with the cost estimate
`docs/specs/2026-11-20-scripps-landscaping-cost-estimate.md`. Every code reference below was read
in the workstation clone at `b0cd4939`; every external count was read live on 2026-11-20.

## Global Constraints

- **Working directory.** The session's default directory is the sshfs mount of production's
  checkout, and neither `cd` nor shell variables carry from one tool call to the next. Every
  shell call in this plan therefore starts with

  ```bash
  cd /home/a/scripps-landscaping && [ "$(git rev-parse --show-toplevel)" = /home/a/scripps-landscaping ] || exit 1
  DC="docker compose -p copi-scripps -f /home/a/scripps-landscaping/docker-compose.scripps.yml"
  ```

  and `docker context show` prints `default`. A command written below without it assumes it.
- All work in the workstation clone `/home/a/scripps-landscaping`, branch `scripps-landscaping`
  (owner, 2026-11-20). Production is read only: its database, containers, checkout
  `~/blackbird-copi-science` (stays on `blackbird`) and `.env` never change.
- `origin` is public. No key values, `.env.scripps`, dumps, logs, addresses or people's names are
  committed. Scrub each diff before `git push --no-tags origin scripps-landscaping`. Never push
  tags.
- Never edit `src/services/jhu_rules.py` or `src/services/nih_reporter.JHU_ORG_EXACT`: PI
  profiles use them.
- `prompts/` is not edited; no prompt-set version bump.
- The tech-transfer source (`jhtv`) stays disabled (C5); `sources/jhtv.py` is not edited.
- Config defaults in `src/config.py` stay as they are; Scripps values live in `.env.scripps`
  (`LANDSCAPE_WINDOW_DAYS=365`, `LANDSCAPE_CUT_FRACTION=0.20`, `LANDSCAPE_DAILY_USD_CAP=100`,
  `LANDSCAPE_NCBI_MAX_RPS=1`). Postgres reads only `.env.scripps-db`.
- Words: "Scripps Research groups" and "Scripps Research labs" (spec §6.2). Identifiers keep
  their names: the `jhu_group_count` column and template key, the `jhtv` kind and source.
- No compose command goes into `scripts/` (`tests/unit/test_operator_commands.py`) and no
  operator text goes into `docs/operations/` (`tests/unit/test_compose_service_references.py`).
- Tests run from `.venv-test` in the clone (Task 0). One full `./scripts/ci.sh`, once, in Task 5,
  with `ANTHROPIC_API_KEY` removed from its environment (`tests/integration/test_cohort_real_llm.py`
  makes paid calls whenever it is set).
- Commits name their files (never a whole directory), so packages never stage each other's work.
  Under `/lathe:plan-execution` each package commits after Task 5's checks pass; run sequentially,
  each task commits after its own. Messages end with the session's attribution lines. Subagents
  use `model: opus`, never Fable.
- During Phase B, progress goes in the run log, not in this file's checkboxes: an edit to a
  tracked file makes the image report `dirty_files` above 0.
- Run artifacts (dry-probe addresses, logs, the dump) live in `/home/a/scripps-run/`, outside the
  repository.

## Review Focus

1. **An authorship tagged `I123431417` whose only address is in Jupiter but carries no
   per-address tag.** A person expects it to be dropped. Pinned in Task 1
   (`test_untagged_jupiter_only_does_not_count`).
2. **A pilot that starts in NCBI's peak hours.** The bake-off must wait for PubMed and GEO, not
   start on the other sources alone. Pinned in Task 2
   (`test_pilot_intake_waits_for_ncbi_slices_at_peak`).
3. **"TSRI" from another organization** (Taiwan Semiconductor Research Institute, in PubMed).
   It must not qualify. Pinned in Task 1 (`test_matcher_fails`).
4. **`start-backfill` run too early or twice.** It must refuse with the route's own words and
   leave the phase alone. Pinned in Task 4 (`test_start_backfill_refused_*`).
5. **A worker that dies mid-slice during the pilot intake** (a crash, the 4 GB limit, a reboot).
   Its `running` fetch row is never cleared, and the bake-off must still start once the slice is
   fetched again. Pinned in Task 2 (`test_pilot_intake_ignores_a_stale_running_row`).

---

## Task 0: Workstation test environment and production snapshot

**Files:** none in the repository. Creates `/home/a/scripps-run/` and `.venv-test/`
(already ignored, `.gitignore:81`).

- [ ] **Step 1: Make the run directory**

```bash
mkdir -p -m 700 /home/a/scripps-run
```

- [ ] **Step 2: Copy the host test venv's versions as constraints** (read-only SSH, `ssh -4`,
  `StrictHostKeyChecking=yes`; never accept a changed host key)

Run on the host: `cd ~/blackbird-copi-science && .venv-test/bin/python -m pip freeze` (if that
venv has no pip, `uv pip freeze --python .venv-test/bin/python`), and save its output locally
without the editable line: `grep -vE '^-e ' > /home/a/scripps-run/host-venv-constraints.txt`.
Expected: about 83 lines, including `anthropic==0.120.2`, `ruff==0.16.1`, `pytest==9.1.1`,
`testcontainers==4.15.0`. If SSH refuses the host key, stop and tell the owner; never accept a
changed key.

- [ ] **Step 3: Build the venv**

```bash
cd /home/a/scripps-landscaping
uv venv --python 3.12 .venv-test
uv pip install --python .venv-test/bin/python -c /home/a/scripps-run/host-venv-constraints.txt -e '.[dev]'
```

Expected: `.venv-test/bin/python -m ruff --version` prints `ruff 0.16.1`.

- [ ] **Step 4: Baseline**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping -q`
Expected: all pass on the unmodified branch. Record the pass count in
`/home/a/scripps-run/notes.md`. A failure here is pre-existing; record it and do not fix it.

- [ ] **Step 5: Snapshot production, read only**

On the host record, into `/home/a/scripps-run/prod-before.txt`: `git -C ~/blackbird-copi-science
rev-parse --abbrev-ref HEAD` (expect `blackbird`), `git --no-optional-locks -C
~/blackbird-copi-science status --porcelain | sha256sum` (no index write),
`sha256sum ~/blackbird-copi-science/.env`, and
`docker ps --filter label=com.docker.compose.project=copi-blackbird --format '{{.Names}} {{.Status}}'`.
Task 10 compares against it.

---

## Task 1: What counts as Scripps Research La Jolla

Spec §6.1. One coherent change: the institution set, the address matcher, the per-address
OpenAlex rule, every source query, the drift listing, and the Hopkins words in these files.

**Files:**
- Modify: `src/landscaping/institutions.py` (whole module)
- Modify: `src/landscaping/sources/openalex_works.py:1-20,106-126,155-157`
- Modify: `src/landscaping/sources/pubmed_works.py:1,14,19,102-113`
- Modify: `src/landscaping/sources/datacite.py:20-27`, `src/landscaping/sources/geo.py:18-27`
- Modify: `src/landscaping/sources/odp_patents.py:1-3,26,36-38,103-111`
- Modify: `src/landscaping/qualify.py:70-71` (docstring only)
- Modify: `src/landscaping/intake.py:54,342-352,362-364` (import and docstrings only)
- Modify: `src/landscaping/reports.py:27,78-80,495-523`
- Modify: `scripts/landscaping_probes.py:38-39` (import), `:138-142`
- Test: `tests/unit/landscaping/test_institutions.py` (rewrite), `test_source_openalex.py`,
  `test_source_pubmed.py`, `test_source_misc.py`, `test_qualify.py`;
  `tests/integration/landscaping/test_reports.py`, `test_intake.py`

**Interfaces:**
- Produces, in `src/landscaping/institutions.py`:
  - `SCRIPPS_OPENALEX_ID: str = "I123431417"`
  - `SCRIPPS_OPENALEX_IDS: frozenset[str] = frozenset({SCRIPPS_OPENALEX_ID})` (replaces
    `BALTIMORE_OPENALEX_IDS`)
  - `EXCLUDED_OPENALEX_IDS: frozenset[str]`, `REPORTER_ORGS: tuple[str, ...]`,
    `NSF_AWARDEE: str`, `ROR_IDS: frozenset[str]` (same names, new values)
  - `names_excluded_campus(text: str) -> bool`
  - `is_scripps_la_jolla_affiliation(text: str) -> bool` (replaces `is_baltimore_affiliation`)
  - `at_scripps_la_jolla(auth: dict) -> bool`
  - `qualifies_openalex(work: dict) -> tuple[bool, str | None, dict | None]` (contract unchanged)
- Produces: `openalex_works.LINEAGE_FILTER == "authorships.institutions.lineage:I123431417"`;
  `pubmed_works.AFFILIATION_TERM == '"Scripps Research"[ad]'`.
- Task 4's dry probe consumes all of the above.

- [ ] **Step 1: Rewrite `tests/unit/landscaping/test_institutions.py`**

```python
import pytest

from src.landscaping.institutions import (
    EXCLUDED_OPENALEX_IDS,
    NSF_AWARDEE,
    REPORTER_ORGS,
    ROR_IDS,
    SCRIPPS_OPENALEX_ID,
    SCRIPPS_OPENALEX_IDS,
    at_scripps_la_jolla,
    is_scripps_la_jolla_affiliation,
    names_excluded_campus,
    qualifies_openalex,
)

LA_JOLLA = "Department of Chemistry, The Scripps Research Institute, La Jolla, CA 92037, USA"
JUPITER = "The Scripps Research Institute, Jupiter, United States"
CALIBR_ID = "I4210140527"


def test_institution_set_matches_spec():
    assert SCRIPPS_OPENALEX_ID == "I123431417"
    assert SCRIPPS_OPENALEX_IDS == frozenset({"I123431417"})
    assert EXCLUDED_OPENALEX_IDS == frozenset({
        "I4387156289", "I4210140527", "I150209017", "I1311914864", "I4210146328",
        "I2802091096", "I4210107570", "I4210103888", "I4210142769", "I4210146979",
        "I4210109568", "I16970960", "I4210089022",
    })
    assert not SCRIPPS_OPENALEX_IDS & EXCLUDED_OPENALEX_IDS
    assert REPORTER_ORGS == ("SCRIPPS RESEARCH INSTITUTE, THE",)
    assert NSF_AWARDEE == "The Scripps Research Institute"
    assert ROR_IDS == frozenset({"02dxx6824"})


@pytest.mark.parametrize("text", [
    LA_JOLLA,
    "Scripps Research, 10550 North Torrey Pines Road, La Jolla, California, USA",
    "Scripps Research Translational Institute, La Jolla, CA, USA",
])
def test_matcher_passes(text):
    assert is_scripps_la_jolla_affiliation(text)


@pytest.mark.parametrize("text", [
    JUPITER,
    "Scripps Research, 130 Scripps Way, Jupiter, FL 33458, USA",
    "Department of Molecular Medicine, Scripps Research, Florida, USA",
    "The Scripps Research Institute, FL 33458",
    "Scripps Health, San Diego, CA, USA",
    "Scripps Clinic, La Jolla, CA, USA",
    "Scripps Institution of Oceanography, UC San Diego, La Jolla, CA, USA",
    "Scripps College, Claremont, CA, USA",
    "The Herbert Wertheim UF Scripps Institute for Biomedical Innovation & Technology, Jupiter, FL",
    "Calibr-Skaggs Institute for Innovative Medicines, The Scripps Research Institute, La Jolla, CA",
    "California Institute for Biomedical Research, La Jolla, CA",
    "Taiwan Semiconductor Research Institute (TSRI), Hsinchu, Taiwan",
    "Scripps",
    "",
])
def test_matcher_fails(text):
    assert not is_scripps_la_jolla_affiliation(text)


def test_excluded_campus():
    assert names_excluded_campus(JUPITER)
    assert names_excluded_campus("Calibr, La Jolla, CA")
    assert not names_excluded_campus(LA_JOLLA)
    assert not names_excluded_campus("")


def _auth(position="last", corresponding=False, inst_ids=(), raw=(), affs=None,
          name="Avery Quillfeather"):
    auth = {
        "author": {"display_name": name, "orcid": None},
        "raw_author_name": name,
        "author_position": position,
        "is_corresponding": corresponding,
        "institutions": [{"id": f"https://openalex.org/{i}"} for i in inst_ids],
        "raw_affiliation_strings": list(raw),
    }
    if affs is not None:
        auth["affiliations"] = [
            {"raw_affiliation_string": text, "institution_ids": [f"https://openalex.org/{i}" for i in ids]}
            for text, ids in affs
        ]
    return auth


S = SCRIPPS_OPENALEX_ID


def test_tagged_la_jolla_counts_and_tagged_jupiter_does_not():
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[(LA_JOLLA, [S])]))
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], affs=[(JUPITER, [S])]))


def test_both_campuses_count():
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[(JUPITER, [S]), (LA_JOLLA, [S])]))


def test_only_tagged_entries_decide():
    # The spec reads OpenAlex's own tags: an untagged La Jolla address beside a tagged Jupiter
    # one does not rescue the authorship.
    untagged = "Department of Chemistry, La Jolla, CA"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], raw=[JUPITER, untagged],
                                         affs=[(JUPITER, [S]), (untagged, [])]))


def test_no_usable_detail_counts():
    assert at_scripps_la_jolla(_auth(inst_ids=[S]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[("", [S])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], raw=["Department of Chemistry, La Jolla"],
                                     affs=[("Department of Chemistry, La Jolla", [])]))


def test_untagged_jupiter_only_does_not_count():
    jupiter_school = "Skaggs Graduate School, Jupiter, United States"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], raw=[jupiter_school],
                                         affs=[(jupiter_school, [])]))


def test_untagged_scripps_strings_decide():
    # With no tagged entry, the strings naming Scripps decide: another institution's address
    # beside a Jupiter one does not rescue the authorship (audit 2026-11-20).
    other = "Max Planck Institute of Biochemistry, Martinsried, Germany"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], raw=[JUPITER, other],
                                         affs=[(JUPITER, []), (other, [])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], raw=[LA_JOLLA, other],
                                     affs=[(LA_JOLLA, []), (other, [])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], raw=[JUPITER, LA_JOLLA]))


def test_calibr():
    calibr = "Calibr-Skaggs Institute for Innovative Medicines, La Jolla, CA"
    assert not at_scripps_la_jolla(_auth(inst_ids=[CALIBR_ID], affs=[(calibr, [CALIBR_ID])]))
    both_ids = "Calibr-Skaggs Institute for Innovative Medicines, The Scripps Research Institute, La Jolla"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S, CALIBR_ID], affs=[(both_ids, [S, CALIBR_ID])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S, CALIBR_ID],
                                     affs=[(calibr, [CALIBR_ID]), (LA_JOLLA, [S])]))


def test_raw_strings_only_when_no_institution():
    assert at_scripps_la_jolla(_auth(raw=[LA_JOLLA]))
    assert at_scripps_la_jolla(_auth(raw=[JUPITER, LA_JOLLA]))
    assert not at_scripps_la_jolla(_auth(raw=["Scripps Health, San Diego, CA"]))
    assert not at_scripps_la_jolla(_auth(inst_ids=["I999"], raw=[LA_JOLLA]))


def _work(*auths):
    return {"id": "https://openalex.org/W1",
            "authorships": [_auth("first", inst_ids=["I999"]), *auths]}


def test_qualifies_openalex():
    last = _auth("last", inst_ids=[S], affs=[(LA_JOLLA, [S])])
    assert qualifies_openalex(_work(last)) == (True, "last", last)
    corr_jupiter = _auth("middle", corresponding=True, inst_ids=[S], affs=[(JUPITER, [S])])
    assert qualifies_openalex(_work(corr_jupiter, last)) == (True, "last", last)
    middle = _auth("middle", inst_ids=[S], affs=[(LA_JOLLA, [S])])
    assert qualifies_openalex(_work(middle)) == (False, None, None)
    collective = _auth("last", inst_ids=[S], affs=[(LA_JOLLA, [S])],
                       name="Invented Cohort Study Consortium")
    assert qualifies_openalex(_work(collective)) == (False, None, None)
```

- [ ] **Step 2: Update the other Task 1 tests**

  - `test_source_openalex.py`: every `"I145311948"` becomes `"I123431417"` (lines 35, 100, 143,
    169). Add:

    ```python
    def test_to_document_keeps_the_qualifying_affiliations():
        affiliations = [{"raw_affiliation_string": "The Scripps Research Institute, La Jolla, United States",
                         "institution_ids": ["https://openalex.org/I123431417"]}]
        senior = {**_auth("last", inst_ids=["I123431417"]), "affiliations": affiliations}
        doc = openalex_works.to_document(_work(11, authorships=[senior]))
        assert doc.role_basis == "last"
        assert doc.raw_qualifier["affiliations"] == affiliations

    def test_to_document_drops_a_jupiter_senior_author():
        senior = {**_auth("last", inst_ids=["I123431417"]), "affiliations": [
            {"raw_affiliation_string": "The Scripps Research Institute, Jupiter, United States",
             "institution_ids": ["https://openalex.org/I123431417"]}]}
        assert openalex_works.to_document(_work(12, authorships=[senior])).role_basis is None
    ```

  - `test_source_pubmed.py`: the qualifying fixture affiliations at lines 42 and 86 become
    `Department of Chemistry, The Scripps Research Institute, La Jolla, CA, USA.`. Rename
    `test_last_author_outside_baltimore_does_not_qualify` to
    `test_last_author_at_jupiter_does_not_qualify`, with affiliation
    `"The Scripps Research Institute, Jupiter, FL 33458, USA"`. In
    `test_existing_parser_output_unchanged`, the expected affiliation at lines 266-267 follows the
    fixture: `"Department of Chemistry, The Scripps Research Institute, La Jolla, CA, USA"` (the
    parser drops the trailing period, as the current expectation shows).
  - `test_source_misc.py`: line 93 asserts `"The Scripps Research Institute" in geo.INST_VARIANTS`.
    `test_odp_query_shape` uses applicant `"The Scripps Research Institute"` and checks
    `f'{field}:"Scripps Research"' in q` for both fields. Replace
    `test_apl_inventor_city_exclusion_when_present` with:

    ```python
    def test_no_inventor_city_excludes_a_scripps_patent():
        for cities in (("Jupiter", "Jupiter"), ("La Jolla", "San Diego"), (None, None)):
            assert odp_patents.to_document(_app(*cities)).raw_qualifier["apl"] is False
        assert odp_patents.to_document(_app("Jupiter", None)).raw_qualifier["inventor_cities"] == ["Jupiter"]
    ```

    `_app` uses applicant `"The Scripps Research Institute"`.
  - `test_qualify.py::test_lab_key_every_record_type`: the OpenAlex fixture uses
    `I123431417` and `"The Scripps Research Institute, La Jolla, CA"`; PubMed affiliation
    `"The Scripps Research Institute, La Jolla, CA"`; RePORTER org
    `"SCRIPPS RESEARCH INSTITUTE, THE"`; NSF awardee `"The Scripps Research Institute"`; ODP
    applicant `"The Scripps Research Institute"`. The assertions do not change. The `apl` S1 test
    at lines 49-52 stays: it builds `raw_qualifier` by hand.
  - `tests/integration/landscaping/test_reports.py`: rename
    `test_drift_lists_unknown_hopkins_institutions` to `test_drift_lists_unknown_scripps_institutions`,
    with institutions `I123431417` "Scripps Research Institute" (in the set), `I4387156289`
    "Scripps Research Invented Florida Annex" (excluded, already decided), `I999000001`
    "Scripps Research Invented Annex", `I999000002` "Invented Lineage Center" with lineage
    `["https://openalex.org/I999000002", "https://openalex.org/I123431417"]`, `I999000003`
    "Invented Other College" and `I999000004` "Scripps Health Invented Clinic". Assert
    `set(listed) == {"I999000001", "I999000002"}`, the annex's two documents and
    `documents_checked == 2`.
  - `tests/integration/landscaping/test_intake.py::test_positive_controls_ingested_by_id`: the
    work's authorship uses `I123431417` and `"The Scripps Research Institute, La Jolla, CA"`;
    the PubMed extras' affiliation is `"Department of Chemistry, The Scripps Research Institute, La Jolla, CA"`.
    The other fixtures there set `role_basis` by hand and stay.

- [ ] **Step 3: Run them to see them fail**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping/test_institutions.py tests/unit/landscaping/test_source_openalex.py -q`
Expected: collection error, `cannot import name 'SCRIPPS_OPENALEX_ID'`.

- [ ] **Step 4: Rewrite `src/landscaping/institutions.py`**

The module docstring names the spec (§6.1). Constants as in the Interfaces block, the excluded
set as in the Step 1 test, one comment per id naming the institution (§6.1 and §12 of the spec
list them). Remove `_ALSO_BALTIMORE`, `_EXCLUDED_PLACE` and the `jhu_rules` import. Patterns:

```python
_SCRIPPS_RESEARCH = re.compile(r"\bscripps\s+research\b", re.IGNORECASE)
_EXCLUDED_CAMPUS = re.compile(
    r"\bjupiter\b|\bflorida\b|\bfl\.?\s*334\d\d\b|\bcalibr\b"
    r"|\bcalifornia\s+institute\s+for\s+biomedical\s+research\b",
    re.IGNORECASE,
)
```

`at_scripps_la_jolla(auth)`, the one algorithm the tests leave open in its order:

```python
inst_ids = {_bare(i.get("id")) for i in auth.get("institutions") or []}
if SCRIPPS_OPENALEX_ID in inst_ids:
    tagged = [a for a in auth.get("affiliations") or []
              if SCRIPPS_OPENALEX_ID in {_bare(x) for x in a.get("institution_ids") or []}]
    if tagged:
        return any(not names_excluded_campus(a.get("raw_affiliation_string") or "") for a in tagged)
    raw = [s for s in auth.get("raw_affiliation_strings") or [] if s]
    return not raw or not all(names_excluded_campus(s) for s in raw)
if inst_ids:
    return False
return any(is_scripps_la_jolla_affiliation(s) for s in auth.get("raw_affiliation_strings") or [])
```

`_bare(url)` is a local helper returning the last path segment (as `authorship_roles._oaid`
does; do not import that private name). `qualifies_openalex` keeps its loop and its
`ROLE_RANK`/`is_individual`/`role` use, and calls `at_scripps_la_jolla(auth)` where it called
`at_institution(...)`; drop the `at_institution` import.

- [ ] **Step 5: Change the sources, intake, reports and the probe script**

  - `openalex_works.py`: `LINEAGE_FILTER = "authorships.institutions.lineage:I123431417"` with
    a comment naming Scripps Research and no child institutions; `raw_qualifier` gains
    `"affiliations": list(authorship.get("affiliations") or [])`; docstrings say "Scripps
    Research" where they say Baltimore.
  - `pubmed_works.py`: `AFFILIATION_TERM = '"Scripps Research"[ad]'`; `_qualify` uses
    `is_scripps_la_jolla_affiliation`; module docstring "Scripps Research La Jolla affiliations".
  - `datacite.py`: `NAME_VARIANTS = ("The Scripps Research Institute", "Scripps Research Institute", "Scripps Research")`.
  - `geo.py`: `INST_VARIANTS` is the same three names.
  - `odp_patents.py`: `_NAMES = ("Scripps Research",)`; `APL_CITIES: frozenset[str] = frozenset()`
    with the comment "No inventor city excludes a Scripps Research patent (spec C3); the S1
    `apl` rule stays and never fires."; docstrings name Scripps Research.
  - `qualify.py`: the `apl` reason in `s1_status`'s docstring becomes "a patent every one of
    whose inventors is in an excluded city (none for Scripps Research)".
  - `intake.py`: import `SCRIPPS_OPENALEX_IDS`; `_institution` tests membership in it; the two
    docstrings say "the Scripps Research institution" and "labs are Scripps Research labs only".
  - `reports.py`: import `SCRIPPS_OPENALEX_IDS`; `_INSTITUTION_NAME = re.compile(r"\bscripps\s+research\b", re.IGNORECASE)`,
    `_LINEAGE_ROOTS = frozenset({"I123431417"})`; rename `_hopkins_like` to `_institution_like`;
    the comment and `drift_report`'s docstring say Scripps Research.
  - `scripts/landscaping_probes.py`: `probe_reporter_pi` uses
    `"org_names": [REPORTER_ORGS[0]]`, imported as
    `from src.landscaping.institutions import REPORTER_ORGS` between the `fulltext` and `sources`
    imports (lines 38-39); the `nih_reporter` import stays (`nih_reporter.BASE`, line 145).

- [ ] **Step 6: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping tests/integration/landscaping/test_reports.py tests/integration/landscaping/test_intake.py -q`
Expected: PASS.

Then: `grep -rnEi 'hopkins|\bjhu\b|baltimore|kennedy|jhpiego' src/landscaping/institutions.py src/landscaping/sources/{openalex_works,pubmed_works,datacite,geo,odp_patents}.py src/landscaping/intake.py src/landscaping/reports.py`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add src/landscaping/institutions.py src/landscaping/qualify.py src/landscaping/intake.py \
  src/landscaping/reports.py src/landscaping/sources/{openalex_works,pubmed_works,datacite,geo,odp_patents}.py \
  scripts/landscaping_probes.py \
  tests/unit/landscaping/{test_institutions,test_source_openalex,test_source_pubmed,test_source_misc,test_qualify}.py \
  tests/integration/landscaping/{test_reports,test_intake}.py
git commit -m "feat(scripps): qualify Scripps Research La Jolla, per-address OpenAlex rule"
```

---

## Task 2: The pilot covers the whole window

Spec §6.3, C2. Package A, after Task 1.

**Files:**
- Modify: `src/landscaping/intake.py:805-817` (`pilot_slices`), `:891-921` (`schedule_due`)
- Modify: `src/landscaping/worker.py:1434-1439` (`_pilot_intake_in`)
- Modify: `src/landscaping/reports.py:710-716` (`_cost_throughput`'s factor and docstring)
- Test: `tests/unit/landscaping/test_qualify.py:5-9,130-149`,
  `tests/integration/landscaping/test_intake.py` (three new tests),
  `tests/integration/landscaping/test_worker.py:872-893` (`test_phase_pilot_runs_no_backfill`)

**Interfaces:**
- Produces: `intake.pilot_slices(*, today: date, seed: int) -> list[tuple[str, str]]` (same
  signature; `seed` kept for callers, unused); `intake.schedule_due(db, *, now: datetime,
  respect_off_peak: bool = True) -> list[tuple[str, str]]`.
- Callers that do not change: `reports._pilot_completeness` (reads `pilot_slices`), the worker's
  intake step (`schedule_due(db, now=now)`), the test fakes (`*args, **kwargs`).

- [ ] **Step 1: Replace the pilot slice test in `test_qualify.py`**

```python
def test_pilot_slices_cover_the_whole_window():
    window = get_settings().landscape_window_days
    slices = pilot_slices(today=TODAY, seed=7)
    expected = [("jhtv", "feed:pilot") if source == "jhtv" else (source, key)
                for source, key in backfill_slices(TODAY, window)]
    assert slices == expected
    assert pilot_slices(today=TODAY, seed=8) == slices
    months = [key for source, key in slices if source == "openalex"]
    for source in ("pubmed", "nsf", "datacite", "geo"):
        assert [k for s, k in slices if s == source] == months
    for source, key in slices:
        assert callable(slice_fetcher(source, key))
```

Import `backfill_slices` alongside `pilot_slices`, wrapped one name per line in ruff's import
style (the one-line form is 105 characters), and drop `timedelta` from the `datetime` import:
the replaced test was its only user.

- [ ] **Step 2: Add two tests to `tests/integration/landscaping/test_intake.py`**

```python
PEAK = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)    # Wednesday, 10:00 Eastern
NIGHT = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)    # Wednesday, 22:00 Eastern


async def test_pilot_ncbi_slices_only_off_peak(db_session):
    db = db_session
    await ensure_sources(db)                         # landscape_phase unset: pilot
    due = await schedule_due(db, now=PEAK)
    assert any(s == "openalex" for s, _ in due)
    assert not any(s in ("pubmed", "geo") for s, _ in due)
    night = await schedule_due(db, now=NIGHT)
    assert [k for s, k in night if s == "pubmed"] == [k for s, k in night if s == "openalex"]
    assert any(s == "geo" for s, _ in night)
    assert any(s == "pubmed" for s, _ in await schedule_due(db, now=PEAK, respect_off_peak=False))


async def test_pilot_intake_waits_for_ncbi_slices_at_peak(db_session):
    from src.landscaping.worker import _pilot_intake_in
    db = db_session
    await ensure_sources(db)
    await _record_fetches(db, await schedule_due(db, now=PEAK), PEAK)
    assert await _pilot_intake_in(db, PEAK) is False
    await _record_fetches(db, await schedule_due(db, now=PEAK, respect_off_peak=False), PEAK)
    assert await _pilot_intake_in(db, PEAK) is True


async def test_pilot_intake_ignores_a_stale_running_row(db_session):
    from src.landscaping.worker import _pilot_intake_in
    db = db_session
    await ensure_sources(db)
    due = await schedule_due(db, now=PEAK, respect_off_peak=False)
    await _record_fetches(db, due, PEAK)
    source, key = due[0]
    db.add(LandscapeFetch(source=source, slice_key=key, status="running",
                          started_at=PEAK - intake.RUNNING_STALE_AFTER - timedelta(minutes=1)))
    await db.flush()
    assert await _pilot_intake_in(db, PEAK) is True
    db.add(LandscapeFetch(source=source, slice_key=key, status="running",
                          started_at=PEAK - timedelta(minutes=5)))
    await db.flush()
    assert await _pilot_intake_in(db, PEAK) is False
```

And rewrite `test_worker.py::test_phase_pilot_runs_no_backfill` as
`test_phase_pilot_fetches_the_whole_window`: keep its setup and the `month:`/`quarter:`/`fy:`/
`feed:` key check; read `anchor_day, _ = await intake._pilot_anchor(db, NOW)` inside the
`ensure_sources` session; assert the fetched OpenAlex keys equal
`[k for s, k in intake.backfill_slices(anchor_day, settings.landscape_window_days) if s == "openalex"]`,
that no fetched source is in `intake.NCBI_SOURCES` (`NOW`, 2026-10-30 12:00 UTC, is 08:00
Eastern on a Friday, peak), that `("jhtv", "feed:pilot")` is not fetched, and that the phase is
still `None`.

- [ ] **Step 3: Run them to see them fail**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping/test_qualify.py tests/integration/landscaping/test_intake.py -q -k "pilot"`
Expected: FAIL (`pilot_slices` returns one month a year; `schedule_due` takes no
`respect_off_peak`).

- [ ] **Step 4: Implement**

  - `pilot_slices`: return `backfill_slices(today, get_settings().landscape_window_days)` with
    `("jhtv", "feed:backfill")` replaced by `("jhtv", "feed:pilot")`; drop `random`'s use here
    (remove the import if nothing else uses it). Docstring: the whole window for the Scripps test
    (spec 2026-11-20 §6.3); the same month keys as the backfill, so its drain finds them fetched.
  - `schedule_due`: new keyword `respect_off_peak: bool = True`. In the `pilot` and `backfill`
    branches, when it is true and `_ncbi_off_peak(now)` is false, drop `NCBI_SOURCES`. Docstring
    names both branches and the keyword.
  - `worker._pilot_intake_in`: `intake.schedule_due(db, now=now + intake.FAILED_RETRY_AFTER,
    respect_off_peak=False)`, and count a `running` row only when
    `started_at > now - intake.RUNNING_STALE_AFTER` (a dead worker's row is never cleared; the
    slice itself is due again after that age, `intake._settled`). Docstring: a slice held back
    for NCBI's off-peak hours is still owed, and a stale `running` row is not a fetch.
  - `reports._cost_throughput`: `factor = 1.0`; its docstring says the pilot covers the whole
    window (Scripps spec §6.3), so nothing is projected. `test_reports.py:316-319` still holds.

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping/test_qualify.py tests/integration/landscaping/test_intake.py tests/integration/landscaping/test_worker.py tests/integration/landscaping/test_reports.py -q`
Expected: PASS. `test_backfill_ncbi_slices_only_off_peak` still passes unchanged.
`test_intake.py` already imports `timedelta`, `intake` and `LandscapeFetch`.

- [ ] **Step 6: Commit**

```bash
git add src/landscaping/intake.py src/landscaping/worker.py src/landscaping/reports.py \
  tests/unit/landscaping/test_qualify.py tests/integration/landscaping/test_intake.py \
  tests/integration/landscaping/test_worker.py
git commit -m "feat(scripps): the pilot fetches the whole window; NCBI off-peak in the pilot too"
```

---

## Task 3: The words the model and the reviewers read

Spec §6.2, C10. Package B. Its tripwire test passes only once Task 1 is merged.

**Files:**
- Modify: `src/landscaping/stages/triage.py:119`, `src/landscaping/stages/deep.py:579`,
  `src/landscaping/stages/final.py:243`
- Modify: `src/services/landscape_chat_record.py:102,365`
- Modify (docstrings): `src/landscaping/ideas.py:284,413,457`, `src/landscaping/scoring.py:122`
- Modify: `templates/workspace/landscape.html:38`, `templates/landscape/_member_idea.html:37`,
  `templates/help/pages/landscape.html:7`, `templates/help/pages/glossary.html:426`,
  `templates/help/tutorials/manager/11-landscape.html:12`,
  `templates/help/tutorials/reviewer/09-landscape.html:12`, `src/web/help_pages.py:85`
- Regenerate: `docs/specs/2026-11-20-landscaping-prompts.md` (by the sync script)
- Create: `tests/unit/landscaping/test_scripps_wording.py`

**Interfaces:** consumes `triage.triage_dossier(idea_fields, evidence, documents) -> str`,
`final._idea_facts(idea) -> dict`, `landscape_chat_record.LEGENDS`/`DOC_VERDICT`,
`help_pages.HELP_PAGES` (each with `slug` and `summary`). Produces the S9 facts key
`scripps_group_count`.

- [ ] **Step 1: Write `tests/unit/landscaping/test_scripps_wording.py`**

```python
"""No Hopkins words where the model or a reviewer reads landscaping text (Scripps spec §6.2)."""
import re
from pathlib import Path
from types import SimpleNamespace

from src.landscaping.stages import final, triage
from src.services.landscape_chat_record import DOC_VERDICT, LEGENDS
from src.web.help_pages import HELP_PAGES

ROOT = Path(__file__).resolve().parents[3]
HOPKINS = re.compile(r"hopkins|\bjhu\b|baltimore", re.IGNORECASE)


def _scanned() -> list[Path]:
    files = [p for p in (ROOT / "src/landscaping").rglob("*.py")
             if p != ROOT / "src/landscaping/sources/jhtv.py"]
    files += [ROOT / "src/services/landscape_chat_record.py", ROOT / "src/services/landscape_view.py",
              ROOT / "templates/help/pages/landscape.html",
              ROOT / "templates/help/tutorials/manager/11-landscape.html",
              ROOT / "templates/help/tutorials/reviewer/09-landscape.html",
              ROOT / "docs/specs/2026-11-20-landscaping-prompts.md"]
    files += sorted((ROOT / "templates/landscape").glob("*.html"))
    files += sorted((ROOT / "templates/workspace").glob("landscape*.html"))
    return files


def test_no_hopkins_words_in_landscaping_text():
    hits = [f"{p.relative_to(ROOT)}:{n}: {line.strip()}"
            for p in _scanned()
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if HOPKINS.search(line)]
    assert len(_scanned()) > 70   # 77 files on 2026-11-20
    assert not hits, "\n".join(hits)


def test_glossary_opportunity_entry_names_scripps():
    text = (ROOT / "templates/help/pages/glossary.html").read_text(encoding="utf-8")
    entry = text.split('id="term-opportunity"', 1)[1].split("</dd>", 1)[0]
    assert "Scripps Research" in entry and not HOPKINS.search(entry)


def test_help_registry_landscape_summary():
    summary = next(p.summary for p in HELP_PAGES if p.slug == "landscape")
    assert "Scripps Research" in summary and not HOPKINS.search(summary)


def test_model_visible_labels():
    dossier = triage.triage_dossier({"idea_type": "biology", "jhu_group_count": 2}, {}, [])
    assert "- Independent Scripps Research groups: 2" in dossier
    facts = final._idea_facts(SimpleNamespace(
        idea_type="biology", targets=None, direction=None, agent=None, disease=None,
        modality_variants=None, stage=None, evidence_grade=None, jhu_group_count=3,
        outside_group_count=None, evidence_summary=None, data_flags=None))
    assert facts["scripps_group_count"] == 3 and "jhu_group_count" not in facts
    assert "Scripps Research labs" in LEGENDS[DOC_VERDICT]
```

The floor is the anti-vacuity check: 61 Python files under `src/landscaping` besides `jhtv.py`,
two services, three help templates, the review document and ten landscape templates.

- [ ] **Step 2: Run it to see it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping/test_scripps_wording.py -q`
Expected: FAIL in all four tests.

- [ ] **Step 3: Change the copy** (exact text; nothing else on these lines changes)

| File | Old | New |
|---|---|---|
| `triage.py:119`, `deep.py:579` | `Independent JHU groups` | `Independent Scripps Research groups` |
| `final.py:243` | `"jhu_group_count": idea.jhu_group_count` | `"scripps_group_count": idea.jhu_group_count` |
| `landscape_chat_record.py:102` | `technology listings of Johns Hopkins labs.` | `technology listings of Scripps Research labs.` |
| `landscape_chat_record.py:365` | `f"Johns Hopkins labs: {idea.jhu_group_count}"` | `f"Scripps Research labs: {idea.jhu_group_count}"` |
| `ideas.py:284,413,457` | `JHU group count` | `Scripps Research group count` |
| `scoring.py:122` | `JHU groups ↓` | `Scripps Research groups ↓` |
| `workspace/landscape.html:38` | `found in Johns Hopkins research,` | `found in research from Scripps Research,` |
| `_member_idea.html:37` | `Johns Hopkins groups:` | `Scripps Research groups:` |
| `help/pages/landscape.html:7` | `reading research from Johns Hopkins` | `reading research from Scripps Research` |
| `glossary.html:426` | `from Johns Hopkins research,` | `from research at Scripps Research,` |
| `manager/11-landscape.html:12` | `reads Johns Hopkins research,` | `reads research from Scripps Research,` |
| `reviewer/09-landscape.html:12` | `reading Johns Hopkins research,` | `reading research from Scripps Research,` |
| `help_pages.py:85` | `found in Johns Hopkins research,` | `found in research from Scripps Research,` |

Re-wrap a help line only where the template's own wrapping needs it.

- [ ] **Step 4: Regenerate the prompt review document** (a generator, run by package B even
  under plan-execution's no-test rule; if it was skipped, Task 5 runs it before its checks)

Run: `.venv-test/bin/python scripts/sync_prompt_set_docs.py`, then the same with `--check`.
Expected: the first rewrites the chat-legend block of
`docs/specs/2026-11-20-landscaping-prompts.md`; `--check` exits 0. No Word re-export (spec
§6.2).

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping/test_scripps_wording.py tests/unit/test_doc_prompt_sync.py tests/unit/landscaping/test_help_rules.py tests/integration/landscaping/test_stage_triage.py tests/integration/landscaping/test_stage_final.py tests/integration/landscaping/test_landscape_chat.py -q`
Expected: PASS once Task 1 is merged; before that, only
`test_no_hopkins_words_in_landscaping_text` fails, on Task 1's files.

The help screenshots do not change: none of the five landscape shots shows these words (all
five PNGs were read on 2026-11-20). Their source hashes go stale; `ci.sh` does not run
`capture --check`.

- [ ] **Step 6: Commit**

```bash
git add src/landscaping/stages/{triage,deep,final}.py src/landscaping/ideas.py src/landscaping/scoring.py \
  src/services/landscape_chat_record.py src/web/help_pages.py \
  templates/workspace/landscape.html templates/landscape/_member_idea.html \
  templates/help/pages/{landscape,glossary}.html \
  templates/help/tutorials/manager/11-landscape.html templates/help/tutorials/reviewer/09-landscape.html \
  docs/specs/2026-11-20-landscaping-prompts.md tests/unit/landscaping/test_scripps_wording.py
git commit -m "feat(scripps): landscaping words name Scripps Research"
```

---

## Task 4: The local stack and the operator scripts

Spec §6.5, §6.6, §7, §8. Package C.

**Files:**
- Create: `docker-compose.scripps.yml`
- Modify: `.gitignore` (add `.env.scripps*`)
- Modify: `src/routers/landscape.py:108-126`
- Create: `scripts/scripps_admin.py`, `scripts/scripps_dry_probe.py`
- Test: `tests/integration/landscaping/test_scripps_admin.py`,
  `tests/unit/landscaping/test_scripps_dry_probe.py`

**Interfaces:**
- Consumes (Task 1): `institutions.SCRIPPS_OPENALEX_ID`, `at_scripps_la_jolla`,
  `qualifies_openalex`, `names_excluded_campus`, `REPORTER_ORGS`; `openalex_works.LINEAGE_FILTER`,
  `_TYPE_FILTER`, `SELECT`, `_complete`; `pubmed_works.AFFILIATION_TERM`.
- Produces, in `src/routers/landscape.py`: `class BackfillRefused(ValueError)` and
  `async def begin_backfill(db: AsyncSession) -> None`, which raises `BackfillRefused` with the
  route's two messages, `f"Backfill starts only from pilot (now {phase})"` and
  `"Backfill needs a stored pilot report"`, else writes `landscape_phase = backfill` without
  committing. `start_backfill` calls it and maps `BackfillRefused` to `HTTPException(409, str(exc))`.
- Produces, in `scripts/scripps_admin.py` (each function commits):
  - `async def grant(db, *, orcid: str, role: str, name: str) -> User`: role `admin`, `manager` or
    `reviewer` (spec C7 gives the colleague `manager`); ORCID through
    `pi_onboarding.validate_orcid`; name stripped and non-empty. A new row gets
    `access_status="allowed"`, the role, and `onboarding_complete = role == "admin"`. An existing
    row gets the role, `allowed`, `onboarding_complete=True` when admin (never reset to False),
    and `bump_session_epoch` when its role changed.
  - `async def start_backfill(db) -> None`: `begin_backfill`, then commit.
  - `async def override_epoch(db, *, epoch_id: uuid.UUID, actor_orcid: str, reason: str) -> None`:
    actor by ORCID (must exist and be an admin, as the route requires), reason stripped and
    non-empty, then `epochs.override`.
  - `async def disable_source(db, *, source: str, reason: str) -> None`: `intake.ensure_sources`
    first; source in `intake.SOURCES`; reason non-empty; prints `source <name> disabled: <reason>`.
  - `async def batch_probe(client, model: str) -> tuple[str, str]`: creates a one-request batch
    (`max_tokens` 1, custom id `scripps-batch-probe`) and cancels it: `("api", batch_id)`; an
    `anthropic.APIStatusError` with status 400, 403 or 404 gives `("sync", message)`; others raise.
  - `def main(argv: list[str] | None = None) -> int`: subcommands
    `grant --orcid --role {admin,manager,reviewer} --name`,
    `start-backfill`, `override-epoch --epoch --actor-orcid --reason`,
    `disable-source NAME --reason`, `batch-probe`. Exit 0 on success, 2 on a `ValueError`
    (message on stderr). Uses `src.database.get_session_factory()` as `landscaping_probes` does;
    `batch-probe` builds `anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)` with
    `settings.landscape_screen_model`.
- Produces, in `scripts/scripps_dry_probe.py`:
  - `def senior_rejected_by_campus(auth: dict) -> bool`: a corresponding or last, individual
    authorship that lists `I123431417` but fails `at_scripps_la_jolla`.
  - `def summarize(works: list[dict], *, sample: int = 20, seed: int = 0) -> dict` with keys
    `openalex_works`, `qualifying_works`, `campus_rejected_works`, `accepted_addresses`,
    `rejected_addresses`. An accepted address is the qualifying authorship's `I123431417`-tagged
    raw strings that `names_excluded_campus` does not flag, else its raw strings, joined with
    `" | "`. A
    rejected address is the first campus-rejected senior authorship's tagged raw strings (its
    raw strings when none is tagged), for a work that does not qualify. ("Pass" above means `names_excluded_campus` does not flag the
    string.) Emails become `<email>`. Samples use `random.Random(seed).sample`.
  - `async def collect(today: date, window_days: int) -> dict`: pages the OpenAlex window
    (`LINEAGE_FILTER`, `_TYPE_FILTER`, publication dates) through `openalex.iter_works` and
    `openalex_works._complete`, then adds `pubmed_count` (ESearch count of `AFFILIATION_TERM` over
    the window by `[dp]`), `reporter_rows` per fiscal year the window touches
    (`reporter._reported_total`), `nsf_awards` (`nsf.fetch_nsf(...).expected`) and the window's
    dates.
  - `def main(argv=None) -> int`: prints `collect`'s JSON (indent 1, sorted keys) for
    `date.today()` and `settings.landscape_window_days`.

- [ ] **Step 1: Write `tests/unit/landscaping/test_scripps_dry_probe.py`**

```python
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
```

- [ ] **Step 2: Write `tests/integration/landscaping/test_scripps_admin.py`** (uses
  `db_session`; `epochs.override` and the script's commits are savepoint releases there)

Tests, each with its assertions:
- `test_grant_creates_an_allowed_admin`: `grant(db, orcid="0000-0002-1825-0097", role="admin",
  name="Invented Owner")` → one `User` row with that ORCID, `user_role == "admin"`,
  `access_status == "allowed"`, `onboarding_complete is True`.
- `test_grant_creates_a_manager_or_reviewer_without_onboarding`: roles `manager` and `reviewer`
  (two ORCID iDs) → `onboarding_complete is False`, the role, `allowed`.
- `test_grant_updates_an_existing_account`: a pending `pi` row made with `tests.factories` (or a
  bare `User`) with that ORCID and `session_epoch=None` → after `grant(..., role="admin")`, still
  one row, `admin`, `allowed`, `session_epoch == 1`.
- `test_grant_refuses_a_bad_orcid_or_role`: `pytest.raises(ValueError)` for ORCID
  `"0000-0002-1825-0098"` (wrong check digit; `…0097` is valid), role `"pi"`, and name `"  "`.
- `test_start_backfill_refused_without_pilot_report`: phase unset → `BackfillRefused` with
  "Backfill needs a stored pilot report"; `landscape_phase` still unset.
- `test_start_backfill_refused_outside_pilot`: `landscape_phase = "continuous"` →
  `BackfillRefused` with "Backfill starts only from pilot (now continuous)".
- `test_start_backfill_moves_pilot_to_backfill`: with a `LandscapeReport(kind="pilot", payload={})`
  → `landscape_phase == "backfill"`.
- `test_override_epoch`: an epoch with `status="held"` and a granted admin → `status ==
  "candidate"`, `override_actor_id == admin.id`, `override_reason == "invented reason"`;
  an unknown actor ORCID, a blank reason and a `manager` actor each raise `ValueError`.
- `test_disable_source`: after `disable_source(db, source="geo", reason="invented")`, the `geo`
  row has `enabled is False`; `source="nope"` raises `ValueError`.
- `test_batch_probe`: a fake client whose `messages.batches.create` returns an object with
  `id="b1"` and whose `cancel` records the id → `("api", "b1")`; a fake whose `create` raises
  `anthropic.PermissionDeniedError("no batches", response=httpx.Response(403, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")), body=None)`
  → `("sync", ...)`.

Import the module as `from scripts import scripps_admin`. `epochs.override` and
`bump_session_epoch` write with bulk `UPDATE`s, so each assertion re-reads its row
(`await db.refresh(row)` or a fresh `select`).

- [ ] **Step 3: Run them to see them fail**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping/test_scripps_dry_probe.py tests/integration/landscaping/test_scripps_admin.py -q`
Expected: collection errors, missing modules.

- [ ] **Step 4: Refactor the backfill route and write the two scripts** to the Interfaces
  block. Each script gets a module docstring with its usage lines, run inside the stack's app
  container (no compose command in the file). Not raw SQL: every write goes through the models
  and the functions named above.

- [ ] **Step 5: Write `docker-compose.scripps.yml`** (exact)

```yaml
# The Scripps landscaping test stack (docs/specs/2026-11-20-scripps-landscaping-design.md
# §6.5). Workstation only: project copi-scripps, the web app on 127.0.0.1:8090, Postgres not
# published. Settings and keys come from the untracked .env.scripps; Postgres reads only the
# untracked .env.scripps-db (POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB).
name: copi-scripps
services:
  postgres:
    image: postgres:15
    restart: unless-stopped
    env_file: .env.scripps-db
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

  scripps-app:
    build:
      context: .
    image: copi-scripps:local
    restart: unless-stopped
    command: ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ports:
      - "127.0.0.1:8090:8000"
    env_file: .env.scripps
    environment:
      # Fail closed whatever .env.scripps says: outside "development" a missing or default
      # SECRET_KEY stops the start (config.Settings._guard_secret_key).
      ENVIRONMENT: production
    volumes:
      - ./prompts:/app/prompts:ro
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen(\\\"http://127.0.0.1:8000/api/health\\\")\""]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s

  landscape-worker:
    image: copi-scripps:local
    pull_policy: never
    restart: unless-stopped
    # Longer than the LLM client's 300 s read timeout, as in production.
    stop_grace_period: 330s
    command: ["python", "-m", "src.landscaping.worker"]
    env_file: .env.scripps
    environment:
      ENVIRONMENT: production
    volumes:
      - ./prompts:/app/prompts:ro
    depends_on:
      postgres:
        condition: service_healthy
    mem_limit: 4g

volumes:
  pgdata:
```

Append `.env.scripps*` to `.gitignore` under "Environment variables (NEVER commit)".

- [ ] **Step 6: Run the tests and validate the compose file**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping/test_scripps_dry_probe.py tests/integration/landscaping/test_scripps_admin.py tests/integration/landscaping/test_pipeline_controls.py tests/unit/test_operator_commands.py tests/unit/test_no_foreign_deployment_names.py tests/unit/test_docker_build_context.py tests/unit/test_repo_hygiene.py -q`
Expected: PASS.

Run, only while neither env file exists yet:
`touch .env.scripps .env.scripps-db; docker compose -f docker-compose.scripps.yml config --quiet; echo $?; rm .env.scripps .env.scripps-db`
Expected: `0`. (Compose refuses a missing `env_file`, hence the empty placeholders.)
Run: `git check-ignore .env.scripps .env.scripps-db` → prints both names.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.scripps.yml .gitignore src/routers/landscape.py scripts/scripps_admin.py \
  scripts/scripps_dry_probe.py tests/unit/landscaping/test_scripps_dry_probe.py \
  tests/integration/landscaping/test_scripps_admin.py
git commit -m "feat(scripps): local compose stack, operator script and dry probe"
```

---

## Task 5: Integrated verification and push

After packages A, B and C are merged.

- [ ] **Step 1: Lint the changed test and script files**

Run: `.venv-test/bin/python -m ruff check --fix tests/unit/landscaping tests/integration/landscaping scripts/scripps_admin.py scripts/scripps_dry_probe.py scripts/landscaping_probes.py`
(import order and wrapping), then the same without `--fix`, plus `ruff check` on the changed
`src/` files: `.venv-test/bin/python -m ruff check src/landscaping/institutions.py src/landscaping/intake.py src/routers/landscape.py`.
Expected: `All checks passed!` for the tests and scripts; no finding in a line this branch
changed.

- [ ] **Step 2: The landscaping and affected suites**

Run: `.venv-test/bin/python -m pytest tests/unit/landscaping tests/integration/landscaping tests/unit/test_doc_prompt_sync.py tests/unit/test_operator_commands.py tests/unit/test_no_foreign_deployment_names.py tests/unit/test_docker_build_context.py tests/unit/test_repo_hygiene.py tests/unit/test_help_shots.py tests/integration/test_help_links_render.py -q`
Expected: PASS. `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check` exits 0.

- [ ] **Step 3: One full gate**

Run: `env -u ANTHROPIC_API_KEY ./scripts/ci.sh 2>&1 | tee /home/a/scripps-run/ci.log` (once; never
two at a time)
Expected: exit 0. A failure is fixed, or shown to fail identically on `origin/blackbird` (run the
same test in a scratch worktree, `git worktree add <scratchpad>/blackbird-baseline origin/blackbird`,
with this venv), and recorded in the run log.

- [ ] **Step 4: Scrub and push**

Run: `git diff origin/scripps-landscaping..HEAD | grep -nEi 'sk-ant|api[_-]?key=|secret=|password=|ec2-|[0-9]{1,3}(\.[0-9]{1,3}){3}|@[a-z0-9.-]+\.[a-z]{2,}'`
Expected: only known hits, each checked by eye: `127.0.0.1` and `0.0.0.0` in the compose file
and the docs, and `someone@example.org` in the dry-probe test. Anything else stops the push.
Then `git push --no-tags origin scripps-landscaping`.

---

## Task 6: Bring up the local stack

Phase B starts here. Every call starts with the Global Constraints' prefix (the clone as working
directory, the guard, `DC`). During the run the log is `/home/a/scripps-run/run-log.md`; Task 10
copies it into the repository.

- [ ] **Step 1: Write the two env files** (mode 600; never printed, never committed)

`.env.scripps-db`: `POSTGRES_USER=copi`, `POSTGRES_PASSWORD=<new>`, `POSTGRES_DB=copi`.
`.env.scripps`: `ENVIRONMENT=production`, a new `SECRET_KEY`,
`DATABASE_URL=postgresql+asyncpg://copi:<the same password>@postgres:5432/copi`,
`BASE_URL=http://localhost:8090`, `ALLOW_HTTP_SESSIONS=true`, `LANDSCAPE_ENABLED=false`,
`LANDSCAPE_DAILY_USD_CAP=100`, `LANDSCAPE_NCBI_MAX_RPS=1`, `LANDSCAPE_BATCH_MODE=sync` (until
Task 7), `LANDSCAPE_WINDOW_DAYS=365`, `LANDSCAPE_CUT_FRACTION=0.20`, `LANDSCAPE_CHAT_ENABLED=false`,
and `LANDSCAPE_SCREEN_MODEL`, `LANDSCAPE_EXTRACT_MODEL`, `LANDSCAPE_TRIAGE_MODEL` all
`claude-opus-5` (spec §6.5: no bake-off candidate, so the funnel stays shut until Task 8 Step 4).
New secrets come from `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` written
straight into the file.
Append production's `NCBI_API_KEY`, `OPENALEX_API_KEY` and `USPTO_API_KEY` lines (production
sets no `NCBI_CONTACT_EMAIL`; checked 2026-11-20) by running
`grep -E '^(NCBI_API_KEY|OPENALEX_API_KEY|USPTO_API_KEY)=' ~/blackbird-copi-science/.env` on the
host over read-only SSH, redirected straight into `.env.scripps`. The owner adds
`ANTHROPIC_API_KEY=<the new key>` themselves.
Check without printing values: `cut -d= -f1 .env.scripps | sort | uniq -d` prints nothing,
`cut -d= -f1 .env.scripps | sort` lists every name above, and
`grep -c '^ANTHROPIC_API_KEY=sk-ant-' .env.scripps` prints `1`.

- [ ] **Step 2: Build from a clean tree**

Run: `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts help_assets pyproject.toml alembic.ini docker-compose.scripps.yml docs`
Expected: no output. `df -h /` shows at least 5 GB free (do not prune the shared build cache).
Run: `$DC build scripps-app`, then `docker run --rm copi-scripps:local cat /app/.build_info.json`
Expected: its commit is `git rev-parse HEAD`, its branch `scripps-landscaping`, `dirty_files` 0.
Run: `docker run --rm copi-scripps:local sh -c 'pip show anthropic httpx | grep -E "^(Name|Version)"; python -c "import src.landscaping.worker, scripts.scripps_admin"'`
Expected: two versions printed (record them in the run log) and no import error.

- [ ] **Step 3: Database**

Run: `$DC up -d postgres`, then `$DC run --rm scripps-app python -m alembic upgrade head`, then
`$DC run --rm scripps-app python -m alembic current`
Expected: `0078 (head)`.

- [ ] **Step 4: Start the app and the worker, disabled**

Run: `$DC up -d scripps-app landscape-worker`; `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/api/health`
Expected: `200`. In psql (`$DC exec postgres psql -U copi -d copi`):
`SELECT source, enabled FROM landscape_sources ORDER BY source;` gives eight rows, only `jhtv`
false; `SELECT status, count(*) FROM landscape_epochs GROUP BY 1;` gives one `candidate`;
`SELECT count(*) FROM landscape_llm_calls;` gives 0.

- [ ] **Step 5: Accounts**

With the owner's ORCID iD and display name, and the colleague's if known:
`$DC run --rm scripps-app python scripts/scripps_admin.py grant --orcid <owner> --role admin --name "<name>"`,
and `--role manager` for the colleague (spec C7). Expected: exit 0 each. The iDs and names go
nowhere but the local database.

---

## Task 7: Pre-run checks and owner gate 1

- [ ] **Step 1: The new key**

Run: `$DC run --rm scripps-app python scripts/scripps_admin.py batch-probe`
Expected: `api …` or `sync …`. Set `LANDSCAPE_BATCH_MODE` to the answer. A `sync` whose message
is not about batch access (an unknown model, say) is investigated before going on.
Run: `$DC run --rm scripps-app python scripts/landscaping_probes.py effort`
Expected: a finding without `"error"`: the key reaches Sonnet 5 and Haiku 5.5 (cents).
The owner confirms in the Anthropic Console that the key's organization has a monthly limit and
rate limits with room for about $265 (the estimate's upper end) over the next week, and whether
it shares them with production.

- [ ] **Step 2: The source keys**

Run: `$DC run --rm scripps-app python scripts/landscaping_probes.py odp-fields`
Expected: a finding with `"records": 1`, not `"error"` (the USPTO key works). The dry probe
next exercises the OpenAlex and NCBI keys.

- [ ] **Step 3: Dry probe**

Run: `$DC run --rm scripps-app python scripts/scripps_dry_probe.py > /home/a/scripps-run/dry-probe.json`
Expected, against spec §12 (2026-11-20): `openalex_works` near 808, `pubmed_count` near 787,
`reporter_rows` 226 for fiscal 2026 and 0 for 2027, `nsf_awards` 0–3. A count more than 20% off
is explained in the run log before going on. `qualifying_works` and `campus_rejected_works` are
new measurements.

- [ ] **Step 4: Start the run log**

`/home/a/scripps-run/run-log.md`: the date, the dry-probe counts, the batch mode, the image's
package versions, and from now on every decision with its reason (C9), the daily spend, the C11
projection and the owner's approvals ("the owner approved on <date>"). Written so it can be
published as it stands: no names, ORCID iDs, addresses or key values.

- [ ] **Step 5: Owner gate 1 (C9)**

The owner reads the 20 accepted and 20 rejected addresses in
`/home/a/scripps-run/dry-probe.json`, looking in particular for a rejected one whose La Jolla
address lacks the word "Scripps" beside a Jupiter Scripps address (the untagged rule's one
known false negative, spec §6.1). Nothing goes on until the owner approves. If the owner
finds La Jolla work rejected or Jupiter work accepted: fix the rule (Task 1), then Task 5 Steps
1–4, Task 6 Step 2, `$DC up -d --force-recreate --no-deps scripps-app landscape-worker`, and
this task's Step 3 again.

---

## Task 8: Pilot, bake-off, checkpoint, picks

- [ ] **Step 1: Enable**

Set `LANDSCAPE_ENABLED=true` in `.env.scripps`; `$DC up -d --force-recreate --no-deps landscape-worker`
(every later recreate uses `--no-deps` too, so Postgres and the app stay up).
Expected: the worker log shows intake starting; PubMed and GEO slices start only from 21:00 to
05:00 US Eastern or at a weekend.

- [ ] **Step 2: Watch the intake** (latest fetch per slice; older failed tries do not count)

```sql
SELECT source, status, count(*) FROM (
  SELECT DISTINCT ON (source, slice_key) source, status FROM landscape_fetches
  ORDER BY source, slice_key, started_at DESC) latest
GROUP BY 1, 2 ORDER BY 1, 2;
```

Until every enabled source shows only `complete` or `incomplete`. A source whose slices keep
failing is disabled with `scripts/scripps_admin.py disable-source <name> --reason "<why>"`,
recorded in the run log (C9).

- [ ] **Step 3: Bake-off**

Wait for `SELECT created_at FROM landscape_reports WHERE kind = 'bakeoff';` to return a row. If
the worker log shows `landscaping step 'evaluation' failed` and no report follows, the worker
does not retry until it restarts (`worker.py:1409-1415`, `state.tried`): record the error, then
`$DC restart landscape-worker`. B below then includes the failed attempt's spend.

- [ ] **Step 4: Checkpoint (C11)**

E = `SELECT count(*) FROM landscape_documents WHERE s1_status = 'eligible' AND merged_into IS NULL;`
B = `SELECT coalesce(sum(cost_usd), 0) FROM landscape_llm_calls WHERE item_ref LIKE 'bakeoff:%';`
P = B + 31.45 + 118.30 × E ÷ 557. Record E, B and P in the run log.
Expected: 151.73 ≤ P ≤ 252.88, and the run goes on. Otherwise **owner gate 2**: send the owner
E, B and P, and wait. Nothing else runs meanwhile: the Opus 5 placeholders keep the funnel shut.

- [ ] **Step 5: Apply the picks**

`SELECT payload->'recommended_settings' FROM landscape_reports WHERE kind = 'bakeoff' ORDER BY created_at DESC LIMIT 1;`
Replace the three placeholders in `.env.scripps` with the picks;
`$DC up -d --force-recreate --no-deps landscape-worker`. Record the picks in the run log, and
whether they differ from production's current models (spec C9).
Expected: the first candidate is retired and a new one created for the new settings
(`epochs.ensure_candidate`); the new candidate's regression runs before any S2 work.

- [ ] **Step 6: The candidate's regression**

`SELECT id, status, hold_reason FROM landscape_epochs WHERE status <> 'retired';`
If it is `held`, summarize its regression run for the owner in the run log, then
`$DC run --rm scripps-app python scripts/scripps_admin.py override-epoch --epoch <id> --actor-orcid <owner> --reason "<why>"`
Expected: status `candidate`, `override_reason` set; the worker log shows S2 starting.

---

## Task 9: From the pilot to cards

- [ ] **Step 1: Pilot report, then backfill**

When `SELECT created_at FROM landscape_reports WHERE kind = 'pilot';` returns a row:
`$DC run --rm scripps-app python scripts/scripps_admin.py start-backfill`
Expected: exit 0; `landscape_phase` becomes `backfill`, then `continuous` once the drain finds
every slice fetched (the same month keys).

- [ ] **Step 2: Activation and the batch**

Expected, in order: `landscape_epochs` shows one `active` (activation can wait for the next UTC
day's cut); `SELECT count(*) FROM landscape_opportunities;` is about 20 (spec §6.7: ~20 at
K = 24); `SELECT id, issued_on FROM landscape_review_batches;` shows one batch. Any further
override is recorded with its reason.

- [ ] **Step 3: Daily spend**

`SELECT (created_at AT TIME ZONE 'UTC')::date AS day, round(sum(cost_usd), 2) FROM landscape_llm_calls GROUP BY 1 ORDER BY 1;`
into the run log. A day over $100 is explained by work started before the cap (spec §6.7).

---

## Task 10: Handover

- [ ] **Step 1: Stop and dump**

Run: `$DC stop landscape-worker` (its 330 s grace lets a call finish), then
`$DC exec -T postgres pg_dump -U copi -d copi -Fc > /home/a/scripps-run/copi-scripps-$(date +%Y%m%d).dump`
Check: `docker run --rm -i postgres:15 pg_restore --list < /home/a/scripps-run/copi-scripps-<date>.dump | head -3`
prints a table of contents. Record its size and SHA-256 in the run log.

- [ ] **Step 2: Production unchanged**

Repeat Task 0 Step 5 and compare with `/home/a/scripps-run/prod-before.txt`.
Expected: identical (the container status column may show a longer uptime, nothing else).

- [ ] **Step 3: Acceptance (spec §8)** — tick each in the run log: matcher and rule tests, wording
  tripwire, ruff, ci.sh, dry probe and owner approval, every source fetched or disabled with a
  reason, bake-off, pilot report, drained, epoch active or overridden with its reason, cards, one
  batch, the daily spend explained, production unchanged, dump made.

- [ ] **Step 4: Publish the run log**

Copy `/home/a/scripps-run/run-log.md` to `docs/plans/2026-11-20-scripps-landscaping-run-log.md`,
re-read it for names, iDs, addresses and keys, scrub as in Task 5 Step 4, then
`git add docs/plans/2026-11-20-scripps-landscaping-run-log.md`, commit
`docs(scripps): run log`, and `git push --no-tags origin scripps-landscaping`.
The owner gets the dump path and spec §7.
