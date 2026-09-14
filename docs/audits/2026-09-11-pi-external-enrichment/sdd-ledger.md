# SDD ledger — plan: docs/plans/2026-09-11-pi-external-enrichment-implementation-plan.md
Branch: feat/pi-external-enrichment from 0abd400. Spec: the two 2026-09-11 analysis docs (read).
Tests run ON THE HOST via ssh (sshfs 100-400x slower). Parallel waves per operator instruction (overrides skill's serial rule).

## Preflight scan
| Pair / task | Shared surface | Finding | Ruling |
|---|---|---|---|
| T1 ↔ T4/T8/T9 | PiGrant/PiIndustry* columns vs `**r.__dict__` inserts | GrantRecord fields ⊂ PiGrant cols; EvidenceItem fields ⊂ PiIndustryEvidence cols — consistent | none |
| T2 ↔ T3 | `JHU_ORG_EXACT` import | T3 imports from T2 → same agent | Ruling: run T2+T3 in one agent |
| T6 ↔ T7 | `industry_sources/__init__.py` (EvidenceItem) | T7 imports T6's package | Ruling: run T6+T7 in one agent |
| T4 ↔ T8 | worker/main.py dispatch | T4 adds both branches with lazy import; T8 adds no dispatch | none |
| T5 ↔ T9 | manager.py, directory.py, pi_detail.html, allowlist test | both edit same files | Ruling: run T5+T9 in one agent after T4+T8 |
| T8 | rec["affiliations"] in pubmed_coi | `_parse_pubmed_xml` emits per-author `affiliations` inside `authors[]`, NOT top-level | Ruling: pubmed_coi reads `rec["coi_statement"]` only; drop the affiliation-suffix scan from Task 6 test/impl (record shape) — COI text is the evidence; affiliations come via OpenAlex company institutions |
| T5/T9 tests | `make_manager/make_pi/login_as` helper names | brief says verify; actual helpers differ | carried into dispatch |
| T11 | `async_session_factory` | fixed to `get_session_factory()()` | done in plan |
| T1 | job enum in models + 0047 ADD VALUE | consistent with 0039 pattern | none |

Waves: W1 = {T1} ∥ {T2+T3} ∥ {T6+T7}; W2 = {T4} ∥ {T8} ∥ {T10} ∥ {T11}; W3 = {T5+T9}; W4 = {T12 + full CI}; final review.
Wave 1 dispatched 2026-09-11T13:03:25Z: T1 (a56d53ce), T2+3 (a5b2989a), T6+7 (a621a286). BASE 0abd400.
T2+T3 DONE: 7b6dc20, 56d9114 (12 tests). Ruling: 56d9114 swept in T6 agent's staged files (shared index) — content intact, attribution mixed; NOT rewriting history mid-wave. Cost if wrong: misattributed commit message only. Future dispatches: commit with pathspec (git commit -m ... -- <paths>).
T6+T7 DONE: content in 56d9114 (T6) + 095fdd0 (T7, swept T1's staged files). Rulings: vendor YAML at src/services/industry_sources/vendor_blocklist.yaml (data/ gitignored); COI regex tightened.
T1 DONE: in 095fdd0 (181 tests incl. test_migration_checks; preflight.py bookkeeping updated). Wave 1 HEAD 095fdd0.
Wave-1 review dispatched (opus, af66c719) over 0abd400..095fdd0. Wave 2 dispatched 2026-09-11T13:16:12Z: T4 (a95b5657), T8 (a7d2069b), T10+11 (a27caf03). BASE 095fdd0.
Wave-1 review: Needs fixes. 5 Important (all industry_sources): #1 _NEG false negative; #2 empty-ORCID author match; #3 ctgov fields/shape mismatch + no fetch→parse test; #4 USPTO assignee/co-applicant filter asymmetry; #5 keyword token floor ≥4 chars. → fix round 1 to T6+7 agent.
Minor (deferred): #6 project_num None dedupe collision + supplement test name; #7 "THE JOHNS HOPKINS UNIVERSITY" normalisation; #9 enrichment.py docstring names a test Task 10 creates; #10 ctgov unguarded c['name']/nct; #11 evidence_from_record kw-only + in_tenure hardcoded True; #12 _pace not re-applied on 429 retry.
Ruling: #8 (no unique on pi_industry_scores.user_id) — intentional: rows are a versioned history, latest-by-computed_at is the read; no upsert planned. Cost if wrong: table growth ~1 row per rescore.
T10+T11 DONE: a5b7b7d, 8e288d0 (3 tests). Note: --apply re-enqueues after completion (by design: re-run = refresh).
T8 DONE: 36f147b (9 tests). Deviations: normalise uses bisect_right; role factor not applied to coi_relationship (matches pubmed_coi emitting pi_role=None); rescore_user(db,user_id,tenure_start=None,primary_field=None).
T4 DONE: 860b0b6 (8 tests). Note: no mid-pipeline commit exists; enqueue placed after step-9 flush inside the single transaction.
Wave-2 review dispatched (opus, a90bab5b) over 095fdd0..860b0b6. Wave 3 dispatched 2026-09-11T13:26:37Z: T5+T9 (a0333109). BASE 860b0b6 (T6/7 fix round may land in between).
T6+7 fix round 1/5: 5 addressed, 0 open (commit 820e03d, 24 tests). Scoped re-review dispatched.
Wave-2 review: Needs fixes. Important: (a) grant_titles wiped by empty RePORTER result; (b) 100.0 percentile for zero-evidence PI when cohort empty; (c) ReporterFirehoseError on common surname kills job; (d) scalar_one_or_none on pending jobs → MultipleResultsFound (grant_enrichment.py + scripts/enqueue_enrichment.py).
Ruling: promote two minors into the T8 fix round because Task 9 builds on them — rescore_user must re-read tenure when arg omitted (else veto rescore writes tenure_start_used NULL); patent_filed is dead weight (class 'unknown' excluded) → exempt patent_filed from the class gate. Cost if wrong: small scorer semantics change, versioned.
Minor (deferred): cohort stale-row pick order; _split_name multi-word surnames; single-worker head-of-line; isolation test "industry" substring too broad.
Fix round 1 dispatched to T4 agent (a,c,d-grant), T8 agent (b + 2 promoted), T10/11 agent (d-script).
T6+7 re-review: all 5 ADDRESSED. Minor (deferred): stopword list narrow for 2-char tokens. Note: implementer verified ctgov dotted fields live via curl (in its report).
Task 1: complete (content in 095fdd0, review clean for T1 items)
Task 2: complete (7b6dc20, review clean)
Task 3: complete (56d9114, review clean; minors deferred #6,#7)
Task 6: complete (56d9114 + fix 820e03d)
Task 7: complete (095fdd0 + fix 820e03d)
Task 10: complete (a5b7b7d; minor deferred: "industry" substring)
T11 fix round 1/5: 1 addressed (ecb0f5b). Will re-review together with T4/T8 fixes in one scoped package.
T8 fix round 1/5: 3 addressed (71b2594, 14 tests).
T4 fix round 1/5: 3 addressed (c956873, 11 tests). Combined scoped re-review of T4/T8/T11 fixes dispatched.
Re-review T4/T8/T11: all 7 ADDRESSED. New LOW/MED: peer cohort includes reason=no_evidence rows (raw_sum 0.0) → include in T9-wave fix or final wave. Task 4, 8, 11: complete pending that.
!! Foreign concurrent session (blackbird-copi-science-97, git author alan) is committing to this branch/tree: 4a04b61 "feat(manager): show every control while impersonating; add Slack Bots page" (08:40). Its uncommitted manager.py/test_manager_views.py edits were swept into my T5 commit 05708a7 (impersonation 403 guards removed on slack provision/activate, "operator decision 2026-09-11" docstring, slack-bots route). Ruling: NOT mine to revert; messaged that session; final review must treat those hunks as external. Cost if wrong: mixed attribution only.
T5+T9 DONE: 05708a7, 60d54ef, df614ee (61+23 tests). Foreign 4a04b61 in range. Wave-3 review + T12 + T8 fix round 2 (peer cohort filter) dispatched 2026-09-11T13:48:26Z.
T8 fix round 2/5: 1 addressed (a66a878, 16 tests). Will be verified in the final whole-branch review rather than a separate scoped re-review (single small hunk). Task 8: complete.
Wave-3 review: Needs fixes. Important: (i) industry veto attributes to impersonated user, no record of real admin; (ii) grant veto can wipe ORCID-seeded grant_titles; (iii) veto never re-exports profiles/public/*.md.
Ruling (i): no DDL this branch; keep vetoed_by_user_id = effective user (branch convention), log one WARNING naming real session holder when impersonating, and record real actor in a `vetoed_via_impersonation_by` note is NOT added (would misuse evidence JSONB). Cost if wrong: audit trail weaker than 0044's second-signature pattern; follow-up migration can add recorded_by.
Ruling (ii): on veto, new titles = derive(remaining); if that is empty, drop only the vetoed grant's title from the existing list (keeps ORCID seed, removes the vetoed title). Cost if wrong: an ORCID title identical to a vetoed RePORTER title is also dropped.
Ruling (iii): call the same markdown export profile_edit uses, best-effort after commit, in both veto? — only the GRANT veto (industry score never reaches the export). Cost if wrong: none material.
Promoted minors into fix: idempotency guard on industry veto; cross-user 404 tests; drop duplicate tenure lookup. Deferred: computed_at tie-break; raw_sum/tenure_start_used template guards; pis.html reason-null branch; unused `request` params.
Out-of-scope security note for final review: 4a04b61 + swept hunks admit impersonated sessions to Slack provisioning (teammate's decision).
T5+9 fix round 1/5: 6 addressed (d152818, 68 tests). Verified in final review (no separate scoped re-review; final reviewer told to check these hunks). Tasks 5, 9: complete.
Task 12: complete (ebb61e6 on teammate's branch; cherry-picked to plan branch as 5183bf5). Teammate switched working tree to feat/sim-panel-charts-readability (branched from d152818). Plan branch feat/pi-external-enrichment = 5183bf5. Final review dispatched.
Final CI started on host worktree /tmp/wt-pee-ci (feat/pi-external-enrichment@5183bf5) → /tmp/ci_pee_final.log
Final review: mergeable after 7-item fix set. Rulings: (1) implement union — RePORTER-derived titles + existing titles not among any RePORTER title for that user (keeps CFF/HHMI ORCID fundings; vetoed RePORTER titles drop) and keep CLAUDE.md "supplements"; (4) delete field-percentile sentence, field_percentile stays NULL/unimplemented (spec A6 deferred, primary_field recorded); (5) fix 0047 box blast radius; (2,3,6,7) as reviewer specified. Foreign Slack-provisioning impersonation hunks: NOT fixed here (teammate's code) — surfaced to operator. Fix wave on branch fix/pee-final-wave in host worktree /home/ubuntu/wt-pee-fix (mounted /home/a/mounts/ubuntu/wt-pee-fix); will fast-forward feat/pi-external-enrichment afterwards.
Final fix wave: 0fb613c on fix/pee-final-wave (136 tests). Scoped re-review dispatched.
Fix-wave re-review: 7/7 ADDRESSED; new MEDIUM (reporter_titles computed post-delete → stale titles stick). Ruling: one minimal extra fix dispatched despite the no-second-wave rule — it is a data-integrity one-liner introduced by the wave; cost if wrong: one more 13-min CI run. LOW (no Retry-After) deferred.
Residual fixed: 1000f89 (137 tests). Final CI restarted on fix/pee-final-wave@1000f89 → /tmp/ci_pee_fix2.log. After green: fast-forward feat/pi-external-enrichment to 1000f89, remove worktrees, delete fix branch.
ALL TASKS COMPLETE. feat/pi-external-enrichment = 1000f89. Final CI: 3639 passed, 94 skipped, CI passed (/tmp/ci_pee_fix2.log). Worktrees removed.
