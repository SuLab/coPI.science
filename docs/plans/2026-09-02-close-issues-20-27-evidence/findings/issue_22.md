# Issue #22 verification — Profile pipeline & write integrity

Verified against `copi-prod` @ `18ba52c` (clean tree), 2026-09-02. All line numbers below are CURRENT (HEAD), located by symbol. Snippets were executed with `.venv-test/bin/python` against the real modules (no network, no DB, no Docker).

Drift note: between the issue's reference tree (`b1d54da`) and HEAD, the only one of the named files that changed is `src/routers/admin.py` (commit `18ba52c`), plus `src/agent/simulation.py`. Every other line number the issue cites is still exact. The two admin.py references drifted: `admin.py:92` → `:105-108`, `admin.py:105` → `:120`; `_load_publication_records` moved from `simulation.py:4565` → `:4679`.

## 1. Summary table

| id | claim | verdict | key evidence | conf |
|---|---|---|---|---|
| V1-15a | `fetch_orcid_works`: `"external-ids": null` → `AttributeError`, parse is outside the try | STILL PRESENT | `src/services/orcid.py:103-109` (try wraps fetch only), `:127`; ran → `AttributeError: 'NoneType' object has no attribute 'get'` | high |
| V1-15b | same for `"title": null` (`:115`) | STILL PRESENT | `orcid.py:115`; ran → `AttributeError` (also for `title.title: null`) | high |
| V1-15c | same chain in `fetch_orcid_grants` (`:92`) | STILL PRESENT | `orcid.py:92`; ran → `AttributeError` for `title: null` and `title.title: null` | high |
| V1-15d | same chain in `fetch_orcid_profile` (`:29-39`) | PARTIALLY (issue overstates) | `orcid.py:29-31` — `name: null` IS guarded (`if name_block else ""`); but `given-names: null`, `emails: null`, `researcher-urls: null`, `organization: null`, `display-index: null` all raise (`:30,:35-39,:58,:61,:68`) | high |
| V1-15e | `int(pub_date["year"]["value"])` on null / non-numeric year (`:124`) | STILL PRESENT | `orcid.py:122-124`; ran → `TypeError` for `value: null`, `ValueError` for `"n.d."` | high |
| V1-15f | step-3 catch zeroes the works list; `lost_evidence` gate partly contains; first-run victims stored ungrounded | STILL PRESENT (accurate; two nuances) | `profile_pipeline.py:107-112` sets `orcid_works=[]`, `works_lookup_failed=True`; gate `:391-392` protects only rows with stored `evidence_pub_count>0` (post-0023) and `synthesis_validated is not False` | high |
| V1-15g | no test pins null tolerance | STILL PRESENT | `tests/contract/test_orcid_contract.py:108-114` uses fully populated `external-ids`; no `None` container anywhere | high |
| V1-16a | no `(user_id, pmid)` unique constraint | STILL PRESENT | `src/models/publication.py:13-41` no `__table_args__`; `alembic/versions/0001_initial.py:121-122` non-unique indexes only; no later migration touches `publications` | high |
| V1-16b | `pmids` built without dedup | STILL PRESENT | `profile_pipeline.py:115`, `:147` (DOI path appends without check); replicated: `['111','111','111']` | high |
| V1-16c | `existing_pubs` never updated inside the insert loop | STILL PRESENT | `profile_pipeline.py:182` built once; loop `:212-229` adds to `new_publications`, never to `existing_pubs` | high |
| V1-16d | `scalar_one_or_none()` → `MultipleResultsFound` swallowed at `logger.debug` | STILL PRESENT | `profile_pipeline.py:272-282` (`except Exception` → `logger.debug`) | high |
| V1-16e | duplicated citation lines in export | STILL PRESENT | `profile_export.py:78-106` — sort/slice, no pmid/doi dedup | high |
| V1-16f | inflated admin counts | STILL PRESENT (line drifted) | `admin.py:105-108` `func.count(Publication.id)` grouped by user, no `DISTINCT pmid` | high |
| V1-16g | `_load_publication_records` full join, duplicates multiply rows, set absorbs | STILL PRESENT (line drifted) | `simulation.py:4679-4709` — plain join, `record.dois.add(...)` | high |
| V1-pm1 | `pubmed.py:207-208` `.text` truncates title at first child | STILL PRESENT | ran `_parse_pubmed_xml` → title `'Role of '`; `"".join(itertext())` → `'Role of TP53 in cancer'` | high |
| V1-pm2 | abstract has the identical defect (`:213-218`) | STILL PRESENT | `pubmed.py:214` `abstract_el.text or ""`; ran → `'BACKGROUND: We studied  Plain '` | high |
| V1-pm3 | no `itertext()` anywhere in `src/` | STILL PRESENT (with a missed helper) | grep: none; BUT `pubmed.py:404-413 _extract_text` is a recursive text collector already used for PMC methods | high |
| V1-pm4 | propagates to `fetch_abstract`/`fetch_full_text` | STILL PRESENT | `pubmed.py:441-453` returns `rec.get("title")` from the same parser | high |
| V1-val1 | `_validate_profile` does `research_summary.split()` on `None` → `AttributeError`; call `:317` not in try | STILL PRESENT | `profile_pipeline.py:561-562`, `:317`; ran → `AttributeError`; `llm._extract_json` (`llm.py:120-167`) is raw `json.loads`, no schema | high |
| V1-val2 | techniques check uses `len()` not `isinstance`; `"PCR"` passes; `:409` assigns str to ARRAY | STILL PRESENT | `:569-570`; ran `techniques="PCR"` → `True`; `:409` `profile.techniques = synthesized.get("techniques", [])` | high |
| V1-val3 | word gate `<100 or >350` disagrees with log + retry prompt ("150-250") | STILL PRESENT | `:563` vs `:565`, `:324`, `:430`; ran: 120 and 300 words both return `True` | high |
| V6-22-gate | pipeline gates overwrite on `validated`/`lost_evidence` ("fixed in stack") | FIXED (confirmed) | `profile_pipeline.py:383-406`; commit `d311170` | high |
| V6-22-flag | `synthesis_validated` persisted (`:414`) | FIXED (confirmed) | `profile_pipeline.py:414`; commit `d311170` | high |
| V6-22a | no web save route resets `synthesis_validated` | STILL PRESENT | grep: only writer is `profile_pipeline.py:414`; consequence chain `:389` | high |
| V6-22b | first-ever run stores unvalidated / zero-evidence unconditionally | STILL PRESENT (deliberate) | `:384-385` requires `profile_version>0`, so first run always takes the `else` at `:407-418` | high |
| V6-22c | `raw_abstracts_hash` written on the discard path | STILL PRESENT | `profile_pipeline.py:372` (before the `if synthesized:` gate) | high |
| V6-pend | `pending_profile` read-but-never-written; sole reader `admin.py` unreachable | STILL PRESENT (line drifted 105→120) | writer grep: none; `admin.py:120`; `templates/profile/view.html:42-48` comment confirms; 4 spec files still describe the flow | high |
| V6-form1 | `Form("")` blanking `agent_page.py:1233-1262` | STILL PRESENT | `agent_page.py:1233-1238` defaults, `:1256-1262` unconditional assigns | high |
| V6-form2 | `Form("")` blanking `profile.py:113-166` | STILL PRESENT | `profile.py:113-118`, `:160-166` | high |
| V6-form3 | `Form("")` blanking `onboarding.py:111-154` | STILL PRESENT | `onboarding.py:111-116`, `:148-154` | high |
| V6-form4 | `profile.py` guards user fields (`if name:`) but not profile fields | STILL PRESENT (and worse) | `profile.py:144-149` vs `:160-165`; `if institution is not None` / `if department is not None` are dead guards (Form("") is never None) | high |
| V6-24a | no `os.replace`/tempfile/`flock` in `src/` | STILL PRESENT | grep over `src/` for `os.replace|tempfile|flock|NamedTemporaryFile|mkstemp`: 0 hits | high |
| V6-24b | truncate-then-write at `profile_export.py:118/:142`, `agent_page.py:1131`, `agent.py:711/:738` | STILL PRESENT (lines exact) | all five `write_text` sites confirmed at those lines | high |
| V6-24c | public writers consistently DB→disk | STILL PRESENT (accurate) | `profile.py:168→185`, `onboarding.py:156→172`, `agent_page.py:1264→1276` (commit then export) | high |
| V6-24d | private save `agent_page.py:1129-1140` disk-first, `if profile:` guard, no `encoding=` | STILL PRESENT (lines exact) | `agent_page.py:1131 profile_path.write_text(content)`, `:1138 if profile:` | high |
| V6-24e | pipeline writes disk `:482` under flush-only txn; commit in `worker/main.py:97` | STILL PRESENT (lines exact) | `profile_pipeline.py:466 flush`, `:482 export`, `:497 flush`; `worker/main.py:97 commit` | high |
| V6-24f | `create_revision` content-dedup ("fixed in stack") | FIXED (confirmed) | `profile_versioning.py:84-92`; commit `da405cb`; pinned by `tests/integration/test_cli.py:731` | high |
| V6-23 | seed written to DB, never exported; agent reads disk only; `export_private_profile` no-ops on empty `private_profile_md` | STILL PRESENT | `profile_pipeline.py:458-464`; `agent.py:119-126`; `profile_export.py:136-137`; no script exports seeds | high |
| C1-a | `profile_version = (x or 0)+1` at 4 sites | STILL PRESENT (lines exact) | `profile_pipeline.py:417`, `profile.py:166`, `onboarding.py:154`, `agent_page.py:1262` | high |
| C1-b | `delegate_slack_ids` whole-column reassign at 3 sites after an awaited Slack lookup | STILL PRESENT (lines exact) | `agent_page.py:1423-1426`, `agent_page.py:1609-1612`, `invite.py:241-244` | high |
| C1-c | `with_for_update` appears once (job claim); no SQL-side increment / `array_append` | STILL PRESENT | `worker/main.py:42` only; grep `array_append|array_remove`: 0 | high |
| DoD | migration test for unique constraint vs pre-existing duplicates | N/A (nothing to test yet) | no constraint, no migration | high |

## 2. Per-item detail

### PR V1 — COR-15 (ORCID null-safety)

Current code (`src/services/orcid.py`):

```
102	    async with httpx.AsyncClient(timeout=30) as client:
103	        try:
104	            resp = await client.get(url, headers=headers)
105	            resp.raise_for_status()
106	            data = resp.json()
107	        except Exception as exc:
108	            logger.warning("Failed to fetch ORCID works for %s: %s", orcid_id, exc)
109	            return []
...
115	                "title": summary.get("title", {}).get("title", {}).get("value", ""),
...
122	            pub_date = summary.get("publication-date", {})
123	            if pub_date and pub_date.get("year"):
124	                work["year"] = int(pub_date["year"]["value"])
...
127	            ext_ids = summary.get("external-ids", {}).get("external-id", [])
128	            for eid in ext_ids:
129	                id_type = eid.get("external-id-type", "").lower()
```

Ran the real functions with `httpx.AsyncClient` patched to return canned payloads:

```
works external-ids=None      -> RAISED AttributeError: 'NoneType' object has no attribute 'get'
works title=None             -> RAISED AttributeError: 'NoneType' object has no attribute 'get'
works title.title=None       -> RAISED AttributeError
works year.value=None        -> RAISED TypeError: int() argument must be ... not 'NoneType'
works year.value='n.d.'      -> RAISED ValueError: invalid literal for int() with base 10: 'n.d.'
works publication-date.year=None -> OK   (this one IS guarded by :123)
works external-id-type=None  -> RAISED AttributeError: 'NoneType' object has no attribute 'lower'   (not in the issue)
works fully populated        -> OK
grants title=None            -> RAISED AttributeError
grants title.title=None      -> RAISED AttributeError
profile name=None            -> OK   (guarded, :30-31 — issue's ":29-39" overstates)
profile given-names=None     -> RAISED AttributeError
profile emails=None          -> RAISED AttributeError
profile researcher-urls=None -> RAISED AttributeError
profile organization=None    -> RAISED AttributeError
profile display-index=None   -> RAISED TypeError   (:58 int(); not in the issue)
```

Try scope: the `try` at `:103-109` wraps only the HTTP fetch/JSON decode; the parse loop `:111-137` is outside it, so any of the above escapes `fetch_orcid_works`. The last commit to `orcid.py` is `1c34878` (display-index sort) — no null-hardening since.

Wrapper in the caller (`profile_pipeline.py:107-112`):
```
107	    try:
108	        orcid_works = await fetch_orcid_works(orcid_id)
109	    except Exception as exc:
110	        logger.warning("Step 3 failed: %s", exc)
111	        orcid_works = []
112	        works_lookup_failed = True
```
So the issue's "step-3 catch zeroes the whole works list" holds. Two nuances the issue did not state:
- `works_lookup_failed=True` makes `evidence_pmid_count=None` (`:380`), so the stored first-run profile carries `evidence_state == "evidence_lost"` — it is flagged, not silent. (`test_profile_pipeline_orcid_works_failure_is_not_reported_as_no_works`, `tests/characterization/test_profile_pipeline_gm.py:728`, pins this for a *raising* stub, which is exactly the AttributeError path.)
- The `lost_evidence` containment (`:391`) requires the STORED row to have `evidence_pub_count > 0`. Pre-0023 rows have `NULL` there (`models/profile.py:44-46`: "legacy rows are NOT backfilled"), so a legacy grounded profile is NOT protected from a refresh that hits this crash — it gets overwritten by a validated, zero-evidence synthesis. The issue's "damage is now partly contained" is therefore narrower than stated.

Tests: `tests/contract/test_orcid_contract.py:104-125` — one work, fully populated `external-ids`, `publication-date`, `title`. No test feeds a null container. `tests/live_api/test_orcid_live.py` inspects live payload shape (needs network; not run).

### PR V1 — COR-16 (publication dedup)

Constraint: `src/models/publication.py:13-41` has no `__table_args__`, `pmid` is `String(20), nullable=True` (`:22`). Migrations: grep of `alembic/versions/*.py` for `publication|pmid|unique` shows only `0001_initial.py:121-122` (`create_index` `ix_publications_user_id`, `ix_publications_pmid`, both non-unique); migrations 0002–0024 never touch `publications`.

Pipeline (`profile_pipeline.py`):
```
115	    pmids = [w["pmid"] for w in orcid_works if w.get("pmid")]
...
146	                    w["pmid"] = resolved_pmid
147	                    pmids.append(resolved_pmid)
...
182	    existing_pubs = {p.pmid: p for p in existing_result.scalars().all() if p.pmid}
...
187	    for rec in pubmed_records:
...
212	        if pmid in existing_pubs:
213	            pub = existing_pubs[pmid]
...
217	        else:
218	            pub = Publication(
...
228	            db.add(pub)
229	            new_publications.append(pub)
```
Replicated the list/loop logic with two ORCID listings sharing PMID 111 plus a DOI-only listing resolving to 111: `pmids == ['111','111','111']`, and the loop (fresh user) would `db.add` three rows because `existing_pubs` is never updated. Note the DOI-only path dedups DOIs (`:127-134`) but not against PMIDs already in `pmids`.

Whether `pubmed_records` actually contains a duplicate for a *same-batch* duplicate id depends on NCBI efetch echoing duplicates — not verifiable offline. Two deterministic paths do not depend on that: (a) the same PMID landing in two different 100-id batches (`pubmed.py:109-110`), (b) a re-run where the same run's flush already inserted the row is fine, but two *concurrent* runs for one user (monthly refresh + manual `/profile/refresh`) both see empty `existing_pubs`.

Downstream, all as claimed:
- `profile_pipeline.py:272-282`: `scalar_one_or_none()` on `(user_id, pmid)` inside `try/except Exception` → `logger.debug(...)`; `MultipleResultsFound` silently drops `methods_text`.
- `profile_export.py:78-106`: filters on `p.title`, sorts by year, slices 20, emits one line per row; no pmid/doi dedup.
- `admin.py:105-108`: `select(Publication.user_id, func.count(Publication.id)).group_by(Publication.user_id)`.
- `simulation.py:4679-4709`: `select(AgentRegistry.agent_id, Publication.doi).join(...)`, accumulates into `record.dois` (a set) — dedup-immune in result, linear in row count.

Other writers of `publications` (relevant to the "IntegrityError discipline" note): `scripts/backfill_publications.py:86` (skips PMIDs already in DB, `:59-74`, but does NOT dedup its own input list — a PMID listed twice in the JSON for one agent is added twice, `:68-97`), `scripts/generate_sparsedata_user.py:576` (dev seed).

Tests: `test_profile_pipeline_rerun_increments_version_and_updates_pubs` (`test_profile_pipeline_gm.py:337`) pins `pub_count_after_two_runs == 2` — i.e. cross-run dedup via `existing_pubs` works. No test feeds duplicate PMIDs within one run. `tests/unit/test_backfill_publications.py` has no duplicate-input case.

### PR V1 — PubMed `.text` truncation

```
207	        title_el = article.find(".//ArticleTitle")
208	        record["title"] = (title_el.text or "") if title_el is not None else ""
...
212	        for abstract_el in article.findall(".//AbstractText"):
213	            label = abstract_el.get("Label")
214	            text = abstract_el.text or ""
```
Ran `_parse_pubmed_xml` on `<ArticleTitle>Role of <i>TP53</i> in cancer</ArticleTitle>` and a labelled abstract with `<i>`/`<sup>`:
```
title   = 'Role of '
abstract= 'BACKGROUND: We studied  Plain '
''.join(itertext()) = 'Role of TP53 in cancer'
```
No `itertext` in `src/` (grep). Missed by the issue: `pubmed.py:404-413 _extract_text(element)` is a recursive text+tail collector already in the module (used for PMC `<sec>`), so the fix is one call away. `fetch_abstract` (`:441-453`) and `fetch_full_text` (`:463-486`) return `rec["title"]`/`rec["abstract"]` from this parser, so the truncated title reaches the agent tools as claimed; DOI (`:190-204`) is unaffected.

Tests: `tests/contract/test_pubmed_contract.py:30,66-67` use plain-text `<ArticleTitle>A Great Paper</ArticleTitle>` and assert the full string — passes with either `.text` or `itertext`; nothing pins markup handling.

### PR V1 — `_validate_profile`

```
561	    research_summary = profile.get("research_summary", "")
562	    word_count = len(research_summary.split())
563	    if word_count < 100 or word_count > 350:
564	        logger.warning(
565	            "Research summary word count %d outside 150-250 range", word_count
...
569	    techniques = profile.get("techniques", [])
570	    if len(techniques) < 3:
```
Ran:
```
research_summary=None -> RAISED AttributeError 'NoneType' object has no attribute 'split'
research_summary=123  -> RAISED AttributeError
techniques='PCR'      -> True
techniques=None       -> RAISED TypeError object of type 'NoneType' has no len()
disease_areas='cancer'-> True
word_count 120        -> True   (message/prompt say 150-250)
word_count 300        -> True
```
Call site `profile_pipeline.py:317 validated = _validate_profile(synthesized)` is not in a try; the retry call `:327` is inside a try that only logs. `synthesize_profile` → `_extract_json` (`llm.py:120-167`) is bare `json.loads` with no schema/type coercion, so `null` and string-typed arrays pass straight through. An escaping exception reaches `worker/main.py:99-111` → retry → `dead` (pinned generically by `tests/integration/test_worker.py:381`). `:409 profile.techniques = synthesized.get("techniques", [])` would hand a `str` to `ARRAY(String)`. Retry prompt `:324` and progress text `:430` both say "150-250".

### PR V6 — COR-22

FIXED as claimed (commit `d311170`): `profile_pipeline.py:383-406` gate (`stored_is_worth_keeping`, `lost_evidence`), `:414 profile.synthesis_validated = validated`. Pinned by `test_profile_pipeline_gm.py:472,532,759`.

Residuals, all STILL PRESENT:
- (a) grep `synthesis_validated` across `src/`: the only assignment is `profile_pipeline.py:414`. None of `profile.py:160-166`, `onboarding.py:148-154`, `agent_page.py:1256-1262` touch it. Consequence per `:389`: a PI-edited draft that was stored with `False` stays `False`, so `stored_is_worth_keeping` is `False` and the next refresh overwrites it even with a synthesis that itself failed validation.
- (b) `:384-385` `(profile.profile_version or 0) > 0` — a first run always falls to `:407-418` and stores whatever came back, including `validated=False` / `evidence_pub_count=0`. Documented as deliberate `:342-366`.
- (c) `:372 profile.raw_abstracts_hash = abstracts_hash` executes before the `if synthesized:` gate — written on the discard path.

`pending_profile`: model columns `models/profile.py:67-70`; sole reader `admin.py:120 elif profile.pending_profile:`; no writer anywhere in `src/` or `scripts/`. `templates/profile/view.html:42-48` carries a comment saying exactly this (banner removed because nothing writes it). Specs still describing the flow: `specs/profile-ingestion.md:186`, `specs/data-model.md:53-60`, `specs/auth-and-user-management.md:144` (plus `specs/tech-stack.md:35` mentions the column) — the issue's "three specs" undercounts by one if tech-stack is included.

`Form("")` blanking — three routes, all unconditional:
```
profile.py:113-118      research_summary/techniques/.../keywords: str = Form("")
profile.py:160-166      profile.research_summary = research_summary ... profile.profile_version = (…)+1
onboarding.py:111-116 / :148-154   same shape
agent_page.py:1233-1238 / :1256-1262   same shape
```
`profile.py:144-149`: `if name:` is a real guard; `if institution is not None:` and `if department is not None:` are dead (a `Form("")` default is never `None`), so `institution or None` blanks those too — the issue's point (user vs profile asymmetry) is right but the asymmetry is narrower than "user fields are guarded". Mitigation of severity: all three templates (`templates/profile/edit.html`, `templates/onboarding/profile_review.html`, `templates/agent/public_profile.html`) render every one of the six fields inside the single form, so a normal browser POST always carries all six; blanking requires a partial/crafted POST or a template regression. Test `tests/integration/test_onboarding_flow.py:837` posts all fields and asserts the version bump — it exercises, but does not pin, the unconditional overwrite.

### PR V6 — COR-24

grep `os\.replace|tempfile|flock|NamedTemporaryFile|mkstemp` over `src/`: zero hits. All file writers are `Path.write_text` (truncate-then-write): `profile_export.py:118`, `:142`; `agent_page.py:1131`; `agent/agent.py:711`, `:738` (plus `grantbot.py:720`, `foa_cache.py:29`, out of scope).

Ordering per writer:
- `profile.py`: `:168 commit` → `:185 export_profile_to_markdown` → `:192 create_revision` → `:200 commit`. DB→disk.
- `onboarding.py save_profile`: `:156 commit` → `:172 export` → `:179 create_revision` → `:188 commit`. DB→disk.
- `agent_page.py save_public_profile`: `:1264 commit` → `:1276 export` → `:1281 create_revision` → `:1290 commit`. DB→disk.
- `agent_page.py save_private_profile` (`:1117-1150`): `:1131 profile_path.write_text(content)` (no `encoding=`) BEFORE `:1133-1140` DB lookup; `:1138 if profile:` — with no `ResearcherProfile` row the write is disk-only and no error is raised; then `create_revision` regardless. Inversion + guard + encoding all as claimed.
- Pipeline: `profile_pipeline.py:466 flush` → `:482 export` → `:489 create_revision` → `:497 flush`; the transaction is committed by the caller at `worker/main.py:97` (after `job.status = "completed"`), and rolled back implicitly on exception (`:99-111`). Disk ahead of DB on any failure between `:482` and the commit.

`create_revision` dedup: FIXED (commit `da405cb`), `profile_versioning.py:84-92` compares against `latest_revision` and returns it unchanged on byte-identical content; pinned by `tests/integration/test_cli.py:731,751`. `tests/unit/test_profile_versioning.py` predates it (model-only tests).

### PR V6 — COR-23

`profile_pipeline.py:458-464` writes `profile.private_profile_seed` only; nothing in `src/` or `scripts/` exports a seed (grep `private_profile_seed`: pipeline, model, onboarding GET `:211` and POST `:269`, tests). `export_private_profile` (`profile_export.py:126-147`) returns `None` when `private_profile_md` is falsy (`:136-137`), so it could not export a seed even if called. Agent reads disk only: `agent.py:119-126` with default `"No private instructions yet."`. Mitigation the issue does not mention: `onboarding.py:211` shows `private_profile_md or private_profile_seed` to the PI, and the POST (`:268-285`) copies it into `private_profile_md` and exports — so the seed reaches disk once the PI completes onboarding step 4. Admin-seeded labs whose PI never logs in run without it, as the issue says.

### PR C1 — RMW races

All seven sites at the issue's exact lines:
```
profile_pipeline.py:417  profile.profile_version = (profile.profile_version or 0) + 1   (row loaded :286-289; awaits at :310, :323, :461 between)
profile.py:166           profile.profile_version = (profile.profile_version or 0) + 1
onboarding.py:154        profile.profile_version = (profile.profile_version or 0) + 1
agent_page.py:1262       profile.profile_version = (profile.profile_version or 0) + 1
agent_page.py:1423-1426  current_ids = list(agent.delegate_slack_ids or []) … agent.delegate_slack_ids = current_ids   (after `await lookup_user_by_email_async` :1417)
agent_page.py:1609-1612  same, remove path (after await :1608)
invite.py:241-244        same, add path (after await :240)
```
`with_for_update`: one hit, `worker/main.py:42` (job claim). `array_append|array_remove`: none. No `version_id_col` on `ResearcherProfile` (`models/profile.py`). No tests exercise concurrent saves or delegate accepts.

## 3. Counts

39 sub-claims assessed: **33 still present**, **3 fixed** (COR-22 gate, COR-22 `synthesis_validated` persist, `create_revision` dedup — all confirmed with commits), **1 partially** (COR-15 `fetch_orcid_profile`: `name: null` is guarded, five sibling containers are not), **0 changed**, **0 not reproducible**, **1 N/A** (unique-constraint migration test — no constraint exists to test), plus 1 "accurate observation" row (public writers are DB→disk) folded into still-present.

Issue text inaccuracies (defect unaffected): `admin.py:92`→`:105-108`, `admin.py:105`→`:120`, `simulation.py:4565`→`:4679` (drift from `18ba52c`); `fetch_orcid_profile :29-39` overstates (name block guarded); "three specs" is four if `tech-stack.md` counts; `profile.py` user-field guards are mostly dead (`is not None` on `Form("")`), which strengthens rather than weakens the point; the issue omits that `pubmed._extract_text` already exists as an itertext-equivalent, and that two extra ORCID null sites raise (`external-id-type: null` → `.lower()`, `display-index: null` → `int()`).

## 4. What I could not verify and why

- Whether NCBI efetch returns two `<PubmedArticle>` elements when the same PMID appears twice in one `id=` batch (no network). The cross-batch and concurrent-run duplicate paths do not depend on this.
- Whether ORCID's public API actually emits `"external-ids": null` / `"title": null` for real records (no network). The code's own `publication-date` guard at `orcid.py:123` and the issue's assertion are the only evidence; the parsers demonstrably raise if it does.
- Live behaviour of the `with_for_update`-free RMW under real concurrency (needs a database; not run per constraints). The code shape is unambiguous.
- `tests/live_api/test_orcid_live.py` shape checks (network-gated).
