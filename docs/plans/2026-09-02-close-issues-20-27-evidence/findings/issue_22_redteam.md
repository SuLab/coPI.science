# Issue #22 — red-team pass (second reviewer)

Tree: `copi-prod` @ 18ba52c, clean. All verdicts re-derived by my own grep/sed and by running the real modules with
`.venv-test/bin/python` (ORCID parsers driven through a fake `httpx.AsyncClient`; PubMed parser on inline XML; validator on dicts).
`git show da405cb^:…` used to read pre-fix `create_revision`. No DB/Docker/network.

## 1. Table

| id | first-agent verdict | red-team result | one-line reason | evidence |
|---|---|---|---|---|
| V1-15a | STILL PRESENT | UPHELD (re-run) | `works external-ids=None -> AttributeError 'NoneType' object has no attribute 'get'`; parse loop `orcid.py:111-137` is outside the try `:103-109` | run below |
| V1-15b | STILL PRESENT | UPHELD (re-run) | `title=None` and `title.title=None` both raise | run |
| V1-15c | STILL PRESENT | UPHELD (re-run) | grants `title=None` / `title.title=None` raise; `:92` | run |
| V1-15d | PARTIALLY | UPHELD (re-run) | `name=None -> OK` (guarded `:30-31`); `given-names/emails/researcher-urls=None` raise | run |
| V1-15e | STILL PRESENT | UPHELD (re-run) | `year.value=None -> TypeError`, `'n.d.' -> ValueError`; `publication-date=None -> OK` (guard `:123`) | run |
| V1-15f | STILL PRESENT | UPHELD | catch `profile_pipeline.py:107-112`; NEW sub-claim (legacy rows unprotected) confirmed: gate `:390 (profile.evidence_pub_count or 0) > 0`; `alembic/versions/0023…:21-22` "deliberately NOT backfilled"; `models/profile.py:44-46` | sed |
| V1-15g | STILL PRESENT | UPHELD | only `None` in `tests/contract/test_orcid_contract.py` is `:46 "end-date": None` (a leaf, not a container) | grep |
| V1-16a | STILL PRESENT | UPHELD | `models/publication.py` no `__table_args__`, `pmid` nullable `:22`; `0001_initial.py:121-122` non-unique; `0023` mentions `publications` only in its docstring `:23-24` | grep |
| V1-16b | STILL PRESENT | UPHELD | `profile_pipeline.py:115`, `:147` | sed |
| V1-16c | STILL PRESENT | UPHELD | `:182` built once; `:212-213` lookup; `:228-229` add without updating dict | sed |
| V1-16d | STILL PRESENT | UPHELD | `:278 scalar_one_or_none()` inside `except Exception` → `logger.debug` `:281-282` | sed |
| V1-16e | STILL PRESENT | UPHELD | `profile_export.py:79-84` sort+`[:20]`, per-row citation, no pmid/doi set | sed |
| V1-16f | STILL PRESENT | UPHELD | `admin.py:107 func.count(Publication.id)` | grep |
| V1-16g | STILL PRESENT | UPHELD (anchor only) | `simulation.py:4679 _load_publication_records`; body not re-read | grep |
| V1-pm1 | STILL PRESENT | UPHELD (re-run) | `title = 'Role of '` | run |
| V1-pm2 | STILL PRESENT | UPHELD (re-run) | `abstract = 'BACKGROUND: We studied  Plain '` | run |
| V1-pm3 | STILL PRESENT (+ missed helper) | QUALIFIED | `_extract_text` (`pubmed.py:404-413`) exists and is unused for title/abstract, but it is NOT itertext-equivalent: it strips and space-joins, so `H<sub>2</sub>O` → `'H 2 O'` vs itertext `'H2O'`. Using it as the fix would corrupt formulas/gene symbols with sub/superscripts | run |
| V1-pm4 | STILL PRESENT | UPHELD | `fetch_abstract` returns `rec.get("title", "")` from the same parser (`pubmed.py:444-445`) | sed |
| V1-val1 | STILL PRESENT | UPHELD (re-run) | `summary=None -> AttributeError`; call `:317` bare (no try) | run/sed |
| V1-val2 | STILL PRESENT | UPHELD (re-run) | `techniques='PCR' -> True`; `:409 profile.techniques = synthesized.get(...)` | run |
| V1-val3 | STILL PRESENT | UPHELD (re-run) | 120 and 300 words → True; message `:565` says 150-250 | run |
| V6-22-gate | FIXED | QUALIFIED (residual sites) | pipeline gate `:383-406` confirmed (d311170) and tests 472/532/759 would fail pre-fix (532 asserts stored summary kept AND `profile_version == 1`); BUT four scripts re-synthesize and overwrite `research_summary` + bump `profile_version` with no `_validate_profile`, no gate: `scripts/resynth_from_current_pubs.py:76-82`, `regen_profile_from_cv.py:112-118`, `vet_publications.py:177-183`, `regen_profiles_from_web.py:107-113` | grep |
| V6-22-flag | FIXED | QUALIFIED (residual sites) | `:414` is the ONLY writer of `synthesis_validated`; the same four scripts write a new synthesis without touching `synthesis_validated` / `evidence_pmid_count` / `evidence_pub_count`, so after a script run the provenance columns describe the previous synthesis (stale, not just unset) | grep |
| V6-22a | STILL PRESENT | UPHELD | writers of `synthesis_validated`: `profile_pipeline.py:414` only | grep |
| V6-22b | STILL PRESENT | UPHELD | `:384-385 (profile.profile_version or 0) > 0` | sed |
| V6-22c | STILL PRESENT | UPHELD | `:372` precedes `if synthesized:` `:383` | sed |
| V6-pend | STILL PRESENT | UPHELD | no `pending_profile =` writer in src/ or scripts/; reader `admin.py:120` | grep |
| V6-form1 | STILL PRESENT | UPHELD | `agent_page.py:1233-1238` six `Form("")`; `:1262` | grep |
| V6-form2 | STILL PRESENT | UPHELD | `profile.py:113-118`, `:160-166` | sed |
| V6-form3 | STILL PRESENT | UPHELD | `onboarding.py:111-116` six `Form("")`; `:154` | grep |
| V6-form4 | STILL PRESENT (and worse) | UPHELD | `profile.py:144-149`: `if name:` real; `if institution is not None` / `if department is not None` always true under `Form("")` → `institution or None` blanks user fields too | sed |
| V6-24a | STILL PRESENT | UPHELD | 0 hits for `os.replace|tempfile|flock|NamedTemporaryFile|mkstemp` in src/ | grep |
| V6-24b | STILL PRESENT | UPHELD | `write_text` at `profile_export.py:118,:142`, `agent_page.py:1131`, `agent.py:711,:738` (+ grantbot/foa_cache out of scope) | grep |
| V6-24c | STILL PRESENT (accurate) | UPHELD | `profile.py:168→185→192→200`; `onboarding.py:156→172→179→188`; `agent_page.py:1264→1274→1281→1289` | grep |
| V6-24d | STILL PRESENT | UPHELD | `agent_page.py:1131 write_text(content)` (no encoding) → `:1138 if profile:` → `:1140 commit` → `:1144 create_revision` | grep |
| V6-24e | STILL PRESENT | UPHELD | `profile_pipeline.py:466 flush → :482 export → :489 create_revision → :497 flush`; commit `worker/main.py:97` | grep |
| V6-24f | FIXED | QUALIFIED (test does not pin it) | fix confirmed `profile_versioning.py:84-92` (da405cb; pre-fix had no `latest_revision` compare); sole `ProfileRevision(` constructor is `:94` so no bypass. BUT the cited test (`test_cli.py:731`) drives `backfill-profile-revisions`, and `cli.py:273-282` performs its OWN `latest_revision`/content compare before calling `create_revision` — the test passes even if `create_revision`'s dedup is removed. `tests/unit/test_profile_versioning.py` has no dedup case (0 hits for unchanged/identical/previous) | sed |
| V6-23 | STILL PRESENT | UPHELD | `profile_pipeline.py:458-462` seed only; `agent.py:119-126` disk-only; `profile_export.py:136-137` returns None on empty md; NEW mitigation confirmed `onboarding.py:211 private_profile_md or private_profile_seed`, POST `:269` clears seed, `:285` exports | sed |
| C1-a | STILL PRESENT | UPHELD | 4 sites exact: `profile_pipeline.py:417`, `onboarding.py:154`, `agent_page.py:1262`, `profile.py:166` | grep |
| C1-b | STILL PRESENT | UPHELD | `agent_page.py:1423-1426` (await `:1416`), `:1609-1612` (await `:1608`), `invite.py:241-244` (await `:239`); `:347` is a read | sed |
| C1-c | STILL PRESENT | UPHELD | `with_for_update` only `worker/main.py:42`; `array_append|array_remove|version_id_col` 0 hits | grep |
| DoD | N/A | UPHELD | no constraint exists | |

## 2. Detail — QUALIFIED rows and NEW claims

### V6-22-gate / V6-22-flag — FIXED in the pipeline, residual in four scripts
`profile_pipeline.py:383-418` gates the overwrite (`stored_is_worth_keeping`, `lost_evidence`) and writes `synthesis_validated` /
`evidence_*` alongside the synthesized fields. The three cited characterization tests would fail on the pre-d311170 code: `:472`
asserts `synthesis_validated is False` (column did not exist), `:532` asserts the stored `_VALID_PROFILE` summary survives and
`profile_version == 1` (pre-fix overwrote and bumped), `:759` asserts `evidence_pub_count == 2 and profile_version == 1`.
Residual the first agent missed: `grep -ln synthesize_profile scripts/` → `resynth_from_current_pubs.py`, `regen_profile_from_cv.py`,
`vet_publications.py`, `regen_profiles_from_web.py`. Each does `profile.research_summary = synthesized.get(...)` and
`profile.profile_version = (profile.profile_version or 0) + 1` with zero hits for `_validate_profile`, `synthesis_validated`,
`evidence_pub_count`, `evidence_pmid_count`. Consequences: (1) an unvalidated script synthesis is stored unconditionally (the COR-22
defect, one layer out); (2) the row's provenance columns now describe a synthesis that is no longer stored — and `stored_is_worth_keeping`
on the next pipeline run reads that stale `synthesis_validated`. The first agent's residual (a) (web routes don't reset the flag) is the
same class; the scripts are worse because they write a *new synthesis*.

### V6-24f — fix real, cited test does not isolate it
`profile_versioning.py:84-92` compares `previous.content == content` and returns `previous`. Pre-fix (`git show da405cb^`) has no such
compare. But `src/cli.py:273-282`:
```
previous = await latest_revision(...)
if previous is not None and previous.content == content:
    ... "Unchanged {profile_type} profile for {agent_id}" ...
await create_revision(...)
```
so `test_backfill_run_twice_does_not_duplicate_any_revision` (`test_cli.py:731`) is satisfied by the CLI-side check alone. A regression
that drops the compare inside `create_revision` (which every web/pipeline caller relies on) would not be caught. Verdict FIXED stands;
"pinned by test_cli.py:731" does not.

### V1-pm3 — `_extract_text` is not an itertext drop-in
```
title   = 'Role of '                       (current .text)
itertext title      = 'Role of TP53 in cancer'
_extract_text title = 'Role of TP53 in cancer'
itertext abstract   = 'We studied TP53 in H2O.'
_extract_text abs   = 'We studied TP53 in H 2 O.'
```
`_extract_text` (`pubmed.py:404-413`) does `" ".join(p.strip() for p in parts if p.strip())` — fine for `<sec>` paragraphs, wrong for
inline markup. The fix should be `"".join(el.itertext())`, not a call to the existing helper.

### NEW claims re-verified
- **lost_evidence protects only `evidence_pub_count>0` rows** — UPHELD. `0023_profile_synthesis_provenance.py:21-24`: "All three are
  nullable and are deliberately NOT backfilled … count(publications) would look like a free win and would be a lie". Gate at `:390`
  uses `(profile.evidence_pub_count or 0) > 0`, so every pre-0023 grounded profile is unprotected against a zero-evidence refresh.
- **Templates always post all six fields** — UPHELD with precision: in all three templates the six inputs sit inside a profile-exists
  block (`profile/edit.html:50-121 {% if profile %}`, `agent/public_profile.html:34-109 {% if profile %}`,
  `onboarding/profile_review.html:60-185 {% elif profile %}`); no per-field conditional. So whenever there is a row to blank, a browser
  POST carries all six; blanking needs a crafted/partial POST.
- **`onboarding.py:211` surfaces the seed** — UPHELD (`content = profile.private_profile_md or profile.private_profile_seed or ""`;
  POST `:268-285` copies into `private_profile_md`, nulls the seed, exports).
- **`profile.py` `is not None` guards dead** — UPHELD (`:108-119` all `Form("")`; `:146-149`).
- **`backfill_publications.py` no input dedup** — UPHELD: `wanted` (`:68`) and `missing` (`:69-74`) keep duplicates; `records` is keyed
  by pmid but the insert loop iterates `missing` (`:77-97`) → two `db.add` for a twice-listed PMID.
- **Extra ORCID null sites** — `external-id-type=None -> AttributeError … 'lower'` reproduced; `display-index` not re-run.

### Reproductions (all `.venv-test/bin/python`, real modules)
```
works fully populated                  OK
works external-ids=None                RAISED AttributeError: 'NoneType' object has no attribute 'get'
works title=None                       RAISED AttributeError
works title.title=None                 RAISED AttributeError
works year.value=None                  RAISED TypeError: int() argument must be ... not 'NoneType'
works year.value='n.d.'                RAISED ValueError: invalid literal for int() with base 10: 'n.d.'
works publication-date=None            OK
works external-id-type=None            RAISED AttributeError: 'NoneType' object has no attribute 'lower'
grants title=None / title.title=None   RAISED AttributeError
profile name=None                      OK  (name falls back to the ORCID id)
profile given-names/emails/researcher-urls=None   RAISED AttributeError
summary=None -> AttributeError; techniques='PCR' -> True; techniques=None -> TypeError; 120 words -> True; 300 words -> True
```

## 3. Mis-cites / wording in the first-agent report
- V1-16d cited `:272-282` as the block; the `scalar_one_or_none()` is at `:278`, `except`/`debug` at `:280-282` — block range fine.
- V6-24f "pinned by `tests/integration/test_cli.py:731,751`": the CLI does its own compare (`cli.py:273-282`); not a pin of
  `create_revision`.
- V1-pm3 "recursive text collector … the fix is one call away": `_extract_text` inserts spaces at element boundaries (shown above).
- V6-22 residual list omits the four `scripts/` synthesizers.
- C1-b await anchors: add path `:1416` (report says `:1417`), invite `:239` (report says `:240`) — off by one, same statements.
- All other cites checked (orcid.py, pubmed.py, profile_pipeline.py, profile.py, onboarding.py, agent_page.py, invite.py,
  profile_export.py, admin.py:107/120, models/publication.py, 0001/0023 migrations, agent.py:119-126) point at the quoted code.

## 4. Counts
40 rows: **36 upheld**, **0 overturned**, **4 qualified** (V6-22-gate, V6-22-flag — residual script writers; V6-24f — cited test does
not isolate the fix; V1-pm3 — helper is not itertext-equivalent), **0 unverifiable** (V1-16g upheld on anchor only, body not re-read).
