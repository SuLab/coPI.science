"""Dead-code guard: every function and method defined in ``src/`` must have a caller
outside ``tests/``, or an ``ALLOWLIST`` entry that says why it needs none.

Nothing else detects this. ruff has no unused-function rule, and coverage counts code
that only tests execute, so a removal that deletes a feature's call sites but keeps its
methods, their tests and their Slack scopes stays green indefinitely (RCA
``docs/audits/2026-09-24-comment-cleanup-rca/README.md`` §8.1, finding I15). This is a
pure AST scan, with no vulture dependency.

What is a definition (scope: all of ``src/``, D15 of docs/plans/2026-09-25-rca-remediation-plan.md):

  * top-level functions, and methods of top-level classes;
  * skipped: dunders, definitions carrying any decorator other than ``staticmethod``,
    ``classmethod``, ``property`` or ``cached_property`` (route handlers, typer
    commands, validators and ``lru_cache`` accessors are called through their
    decorator), and the bodies of ``Protocol`` classes.

What is a reference. The corpus is ``src/``, ``scripts/``, ``alembic/`` and the
identifier tokens of ``templates/**/*.html``. ``tests/`` does not count, and neither
does ``__all__``.

  * A module function ``M.f`` is referenced by a ``Name`` ``f`` in ``M`` itself, by
    ``from M import f`` anywhere (relative imports resolved), or by ``alias.f`` where
    ``alias`` is bound to ``M`` by an import in the referencing module.
  * A method ``name`` of any class is referenced by an attribute access ``.name`` or
    ``.a<name>`` (the async-wrapper convention, ``apost_message`` for
    ``post_message``), by ``getattr(x, "name")``, or by the token ``name`` in a
    template.

How it decides. A reference counts only if it sits in code that is itself live: module
and class-body code, the scripts, alembic, templates and skipped definitions are live
roots; a tracked definition is live only once a counted reference reaches it. The live
set is the least fixpoint of that rule, so a chain or cycle of definitions that only
call each other stays dead.

Known limitations, both false negatives (the guard is deliberately name-based, so it
never needs type inference and never cries wolf over a live call):

  * A dead method that shares its name with any live attribute access is missed —
    ``Dead.close`` is kept alive by an unrelated ``client.close()``. Pinned by
    ``test_control_known_limitation_shared_method_name_is_missed``.
  * A module-level ``from M import f`` counts wherever it sits, so ``f`` stays live
    when the importer only uses it from dead code. The import, not the use, is the
    reference.

``ALLOWLIST`` maps a definition key (``module:function`` or ``module:Class.method``) to
the reason it has no caller in the corpus. The test fails on any unlisted dead
definition and on any stale entry: one whose definition no longer exists, or which has
become live. A stale suppression is the same bug wearing a disguise.
"""

from __future__ import annotations

import ast
import functools
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories, relative to the scanned root, whose Python counts as reference corpus.
# ``src`` is also the only directory whose definitions are tracked.
CORPUS_PY_DIRS = ("src", "scripts", "alembic")
TEMPLATES_DIR = "templates"

_TRANSPARENT_DECORATORS = frozenset(
    {"staticmethod", "classmethod", "property", "cached_property"}
)
_TOKEN_RE = re.compile(r"\w+")

ALLOWLIST: dict[str, str] = {
    "src.main:OriginGuardMiddleware.dispatch": (
        "Starlette BaseHTTPMiddleware hook: the framework calls self.dispatch for every "
        "request; create_app registers the class with add_middleware, never the method."
    ),
    "src.services.blackbird_rubric:parse_rubric": (
        "Public validator for scratch rubric documents (its docstring). The import-time "
        "load reads the bytes once and calls parse_rubric_bytes, so rubric_documents "
        "records exactly the bytes that were parsed (AP-7, Phase 3 Task 27)."
    ),
    # Genuinely dead code the 2026-09-25 integration scan found (§5 step 2 of the plan
    # cited in the module docstring).
    # These are NOT roots: code reachable only from them must still be reported.
    "src.agent.channels:is_seeded_channel": (
        "No caller anywhere, tests included (git grep, 2026-09-25). "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.agent.channels:make_collaboration_channel_name": (
        "No caller anywhere, tests included (git grep, 2026-09-25). "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.agent.channels:normalize_channel_name": (
        "Called only by make_collaboration_channel_name, which is itself dead. "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.agent.channels:record_channel_archived": (
        "No caller anywhere, tests included (git grep, 2026-09-25). "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.agent.ids:TsMinter.writer_id": (
        "Read only by tests/unit/test_ids.py; production code never reads the property. "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.agent.ids:default_writer_id": (
        "Used only by tests/unit/test_ids.py. "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.services.llm:make_decision": (
        "Used only by tests (test_llm_service.py, test_agent_turn_gm.py). "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.services.patents:_tokenise": (
        "Named only in a src/agent/tools.py comment and in tests/unit/test_patents.py. "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.services.patents:clear_prior_art_cache": (
        "Test-support cache reset, used only by tests/unit/test_patents.py. "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    "src.agent.ids:mint_local_ts": (
        "Used only by tests/unit/test_ids.py since its one production caller, "
        "src/services/pi_inbox.py, was retired with the proposal flow. "
        "Kept: the process-wide default minter is still claimed by set_default_writer_id."
    ),
    "src.services.validators:csv_safe_cell": (
        "No CSV export exists in src/ any more (git grep -i csv, 2026-09-25); used only by tests/unit/test_validators.py. "
        "Outside the 2026-09-24 RCA's scope, so kept for now; deletion is follow-up 2026-09-25/R-dead-code in docs/audits/open-findings.md."
    ),
    # Phase 2 (docs/plans/2026-09-29-audit-remediation-phase-2.md) lands some helpers
    # a task before their caller. Each entry names the task that brings the caller;
    # that task removes the entry (the stale-entry test fails until it does).
    "src.services.advisory_locks:SessionAdvisoryLock.backend_pid": (
        "Diagnostic accessor: the backend pid holding the session lock, read by "
        "tests/integration/test_engine_liveness.py to prove liveness follows the "
        "lock-holding connection. No production reader is planned."
    ),
    # Landscaping (docs/plans/2026-10-30-blackbird-landscaping-plan.md, Global Constraints,
    # "Dead-code guard"): the package has no live root until the worker (Task 6.5) and the
    # routes (Phase 7). Each phase's verification step adds an entry naming the task that
    # brings the caller and removes the ones reported stale; none remains after Phase 7.
    "src.landscaping.prompt_set:system_prompt": (
        "Caller lands in Task 4.4 (stage runners); live once the landscaping worker (Task 6.5) is the first live root."
    ),
    "src.landscaping.batches:_Reconciler.rearm": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_adopt": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_advance": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_all_ended": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_arm_startup_scan": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_batches_client": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_close": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_create": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_custom_id": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_first_seq": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_ingest": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_list_candidates": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_list_since": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_mark_ended": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_match_prefixes": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_move_stage_rows": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_outcome_kind": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_parse_custom_id": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_record_chunk": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_record_fallback_failure": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_resolve_row": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_run_one_fallback": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_store_created": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_take": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_touch": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:_uuid_array": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:apply_result": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:mark_ingested": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:poll": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:reconcile_orphans": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:run_fallbacks": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:submit": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.batches:transition": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_build_source": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_copy_idea_evidence": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_epoch_id": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_old_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_old_idea": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_ordered": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_phase": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_retire": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_set_unactivated": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_unactivated": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_upstream_closed": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:_work_pending": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:activate": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:active_epoch": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:changed_stages": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:copy_forward": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:current_components": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:ensure_candidate": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:hold": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:mark_drained": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.epochs:override": (
        "Caller lands in Task 7.3 (the epoch override route)."
    ),
    "src.landscaping.intake:_apply_fields": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_apply_ids": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_apply_qualification": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_apply_summary": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_backfill_slices": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_clean_ids": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_continuous_slices": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_fiscal_year": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_from_openalex": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_hash": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_institution": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_jsonable": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_link": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_link_preprint": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_live": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_matching_rows": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_merge_fields": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_merged_type": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_month_bounds": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_month_slices": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_ncbi_off_peak": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_norm_title": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_openalex_control": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_parse_controls": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_pilot_anchor": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_precedence": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_pubmed_control": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_qualified_by": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_qualifying_authorships": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_quarter_of": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_range_fetch": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_ratio": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_record_from_row": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_refresh_grant_pmids": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_refresh_s1": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_repoint_links": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_rerun_judgments": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_resolve_lab": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_row_ids": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_senior_name": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_setting": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_settled": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_store_rows": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_take_qualification": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_title_expr": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_update_fetch": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:_window_months": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:canonical_id": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:ensure_sources": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:find_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:ingest_positive_controls": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:link_datasets": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:link_grants": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:merge_documents": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:pilot_slices": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:run_slice": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:schedule_due": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:slice_fetcher": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:summary_of": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.intake:upsert_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.ledger:over_daily_cap": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.ledger:spend_today": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.llm_client:build_request": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:_day": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:_make_client": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:_published": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:_query": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:_related": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:_summary": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:fetch_datacite": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.datacite:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.geo:_pdat": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.geo:_search": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.geo:_summaries": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.geo:fetch_geo": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.geo:term": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.geo:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:_author": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:_case_id": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:_child": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:_local": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:_make_client": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:_pub_date": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:_text": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:fetch_rss": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:parse_rss": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.jhtv:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.nsf:_make_client": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.nsf:_mdy": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.nsf:_pi_display_name": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.nsf:fetch_nsf": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.nsf:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_api_key": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_assignees": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_day": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_inventors": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_is_apl": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_make_client": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_names": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:_quarter": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:fetch_quarter": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.odp_patents:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_bare_id": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_complete": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_extra": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_ids": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_license": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_pmcid": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_published": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:_senior": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:fetch_slice": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.openalex_works:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:_date_range": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:_doc_type": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:_fetch_term": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:_qualify": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:_senior_names": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:fetch_edat": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:fetch_month": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.pubmed_works:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:_day": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:_pi_display_name": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:_pis": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:_primary": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:_reported_total": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:_senior": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:fetch_fiscal_year": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.sources.reporter:to_document": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.openalex:_contact_params": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.openalex:fetch_work": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.openalex:iter_works": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:_article_extras": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:_element_date": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:_last_individual_author": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:_month_number": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:_published": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:esearch_page": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:fetch_pubmed_xml": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:parse_pubmed_extras": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.services.pubmed:set_min_interval": (
        "Caller lands in Task 6.5: the landscaping worker is the package's first live root."
    ),
    "src.landscaping.prompt_set:chat_prompt_matches": (
        "Caller lands in Task 7.4 (the landscaping chat router)."
    ),
    "src.landscaping.prompt_set:_criteria_vocab": (
        "Helper of system_prompt; caller lands in Task 4.4, live with Task 6.5."
    ),
    "src.landscaping.prompt_set:_deal_breakers": (
        "Helper of system_prompt; caller lands in Task 4.4, live with Task 6.5."
    ),
    "src.landscaping.prompt_set:_target_rubric": (
        "Helper of system_prompt; caller lands in Task 4.4, live with Task 6.5."
    ),
    "src.landscaping.schema_check:schema_ready": (
        "Callers land in Task 6.5 (worker start) and Task 7.5 (the landscape pages)."
    ),
    "src.services.advisory_locks:landscape_rerank_lock_key": (
        "Caller lands in Task 5.3 (S7 insertion); live once the landscaping worker (Task 6.5) is the first live root."
    ),
}


@dataclass(frozen=True)
class Definition:
    key: str
    module: str
    name: str
    is_method: bool
    path: str
    lineno: int


@dataclass
class ScanResult:
    definitions: dict[str, Definition] = field(default_factory=dict)
    # Dead with the scan's ``roots`` treated as live (an allowlisted entry point's own
    # callees are live), and dead with no roots at all. The second answers "is this
    # allowlist entry still needed?", which seeding would otherwise always say yes to.
    dead: set[str] = field(default_factory=set)
    dead_unseeded: set[str] = field(default_factory=set)


def _module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_tracked(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    name = node.name
    if name.startswith("__") and name.endswith("__"):
        return False
    return all(_decorator_name(d) in _TRANSPARENT_DECORATORS for d in node.decorator_list)


def _is_protocol(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Subscript):
            base = base.value
        if _decorator_name(base) == "Protocol":
            return True
    return False


def _resolve_from(module: str, is_package: bool, node: ast.ImportFrom) -> str:
    """Absolute module named by a ``from ... import``, relative levels resolved."""
    if not node.level:
        return node.module or ""
    package = module if is_package else module.rpartition(".")[0]
    parts = package.split(".") if package else []
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    if node.module:
        parts.append(node.module)
    return ".".join(parts)


@dataclass
class _Module:
    name: str
    rel: str
    tree: ast.Module
    is_package: bool
    # id(def node) -> definition key, for the tracked definitions of a src module.
    tracked: dict[int, str] = field(default_factory=dict)
    # Import-bound alias -> the module it names.
    bindings: dict[str, str] = field(default_factory=dict)


def _load_modules(root: Path) -> list[_Module]:
    modules = []
    for top in CORPUS_PY_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root)
            modules.append(
                _Module(
                    name=_module_name(rel),
                    rel=rel.as_posix(),
                    tree=ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
                    is_package=path.name == "__init__.py",
                )
            )
    return modules


def _collect_definitions(mod: _Module, result: ScanResult) -> None:
    def add(node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str, is_method: bool):
        key = f"{mod.name}:{qualname}"
        mod.tracked[id(node)] = key
        result.definitions[key] = Definition(
            key, mod.name, node.name, is_method, mod.rel, node.lineno
        )

    for node in mod.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tracked(node):
            add(node, node.name, is_method=False)
        elif isinstance(node, ast.ClassDef) and not _is_protocol(node):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tracked(
                    item
                ):
                    add(item, f"{node.name}.{item.name}", is_method=True)


def _collect_bindings(mod: _Module, module_names: set[str]) -> None:
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    mod.bindings[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    mod.bindings[head] = head
        elif isinstance(node, ast.ImportFrom):
            source = _resolve_from(mod.name, mod.is_package, node)
            for alias in node.names:
                target = f"{source}.{alias.name}" if source else alias.name
                if target in module_names:
                    mod.bindings[alias.asname or alias.name] = target


class _RefCollector(ast.NodeVisitor):
    """Records ``(target key, containing tracked key or None)`` for one module."""

    def __init__(self, mod, functions_by_module, methods_by_name, credits):
        self.mod = mod
        self.own_functions = functions_by_module.get(mod.name, {})
        self.functions_by_module = functions_by_module
        self.methods_by_name = methods_by_name
        self.credits = credits
        self.container: str | None = None

    def _credit(self, key: str) -> None:
        self.credits.setdefault(key, set()).add(self.container)

    def _credit_methods(self, name: str) -> None:
        for key in self.methods_by_name.get(name, ()):
            self._credit(key)

    def _visit_def(self, node) -> None:
        key = self.mod.tracked.get(id(node))
        if key is None:
            self.generic_visit(node)
            return
        outer, self.container = self.container, key
        self.generic_visit(node)
        self.container = outer

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def

    def _dotted(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.mod.bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def visit_Name(self, node: ast.Name) -> None:
        key = self.own_functions.get(node.id)
        if key is not None:
            self._credit(key)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._credit_methods(node.attr)
        if node.attr.startswith("a"):
            self._credit_methods(node.attr[1:])
        owner = self._dotted(node.value)
        if owner is not None:
            key = self.functions_by_module.get(owner, {}).get(node.attr)
            if key is not None:
                self._credit(key)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            self._credit_methods(node.args[1].value)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        source = _resolve_from(self.mod.name, self.mod.is_package, node)
        functions = self.functions_by_module.get(source, {})
        for alias in node.names:
            key = functions.get(alias.name)
            if key is not None:
                self._credit(key)


def scan(root: Path, roots: frozenset[str] = frozenset()) -> ScanResult:
    """Scan the tree at ``root`` (a repo root, or a synthetic one in a control case).

    ``roots`` are definition keys to treat as live: the allowlisted entry points, whose
    bodies' references must count. ``dead_unseeded`` repeats the fixpoint without them.
    """
    result = ScanResult()
    modules = _load_modules(root)
    for mod in modules:
        if mod.rel.startswith("src/"):
            _collect_definitions(mod, result)
    module_names = {mod.name for mod in modules}
    for mod in modules:
        _collect_bindings(mod, module_names)

    functions_by_module: dict[str, dict[str, str]] = {}
    methods_by_name: dict[str, set[str]] = {}
    for key, d in result.definitions.items():
        if d.is_method:
            methods_by_name.setdefault(d.name, set()).add(key)
        else:
            functions_by_module.setdefault(d.module, {})[d.name] = key

    credits: dict[str, set[str | None]] = {}
    for mod in modules:
        _RefCollector(mod, functions_by_module, methods_by_name, credits).visit(mod.tree)

    templates = root / TEMPLATES_DIR
    if templates.is_dir():
        tokens: set[str] = set()
        for path in templates.rglob("*.html"):
            tokens.update(_TOKEN_RE.findall(path.read_text(encoding="utf-8")))
        for name in tokens & methods_by_name.keys():
            for key in methods_by_name[name]:
                credits.setdefault(key, set()).add(None)

    def fixpoint(seed: set[str]) -> set[str]:
        live = set(seed)
        changed = True
        while changed:
            changed = False
            for key, containers in credits.items():
                if key not in live and any(c is None or c in live for c in containers):
                    live.add(key)
                    changed = True
        return live

    result.dead = set(result.definitions) - fixpoint(set(roots) & set(result.definitions))
    result.dead_unseeded = set(result.definitions) - fixpoint(set())
    return result


def allowlist_problems(
    result: ScanResult, allowlist: dict[str, str]
) -> tuple[list[str], list[str]]:
    """``(unlisted dead keys, stale allowlist keys)``, each sorted.

    An entry is stale when it is undefined, or live without being seeded as a root.
    """
    unlisted = sorted(result.dead - allowlist.keys())
    stale = sorted(k for k in allowlist if k not in result.dead_unseeded)
    return unlisted, stale


def _describe(result: ScanResult, key: str) -> str:
    d = result.definitions.get(key)
    return f"{key}  ({d.path}:{d.lineno})" if d else f"{key}  (no longer defined)"


# The allowlisted entry points the framework calls by itself. Only these seed the
# fixpoint as live roots, so their callees count as live. The dead-code entries in
# ALLOWLIST are deliberately not roots: seeding them would hide whatever only they
# reach.
ENTRY_POINTS: frozenset[str] = frozenset(
    {
        "src.main:OriginGuardMiddleware.dispatch",
    }
)


@functools.cache
def _repo_scan() -> ScanResult:
    return scan(REPO_ROOT, roots=ENTRY_POINTS)


def test_src_has_no_unlisted_dead_definitions():
    result = _repo_scan()
    unlisted, _ = allowlist_problems(result, ALLOWLIST)
    assert not unlisted, (
        "Definitions in src/ with no live caller outside tests/. Delete them (with their "
        "tests, fakes and docs), or add an ALLOWLIST entry giving the reason:\n  "
        + "\n  ".join(_describe(result, k) for k in unlisted)
    )


def test_allowlist_has_no_stale_entries():
    result = _repo_scan()
    _, stale = allowlist_problems(result, ALLOWLIST)
    assert not stale, (
        "ALLOWLIST entries that are no longer defined or now have a live caller; remove "
        "them:\n  " + "\n  ".join(_describe(result, k) for k in stale)
    )


def test_every_allowlist_entry_has_a_reason():
    assert [k for k, reason in ALLOWLIST.items() if not reason.strip()] == []


def test_every_entry_point_is_allowlisted():
    assert ENTRY_POINTS <= ALLOWLIST.keys(), sorted(ENTRY_POINTS - ALLOWLIST.keys())


# --- Control cases: the scanner against synthetic trees -------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def test_control_an_allowlisted_root_keeps_its_callees_live(tmp_path):
    files = {
        "src/web.py": """
            def helper(): pass
            class Hook:
                def dispatch(self):
                    helper()
        """,
    }
    unseeded = scan(_tree(tmp_path, files))
    assert unseeded.dead == {"src.web:Hook.dispatch", "src.web:helper"}

    seeded = scan(tmp_path, roots=frozenset({"src.web:Hook.dispatch"}))
    assert seeded.dead == set()
    # The root is still needed: without the seed it would be dead, so it is not stale.
    assert allowlist_problems(seeded, {"src.web:Hook.dispatch": "framework hook"}) == ([], [])
    # A root that is also live on its own merit is stale.
    live_files = {"src/web2.py": "def entry(): pass\nentry()\n"}
    both = scan(_tree(tmp_path, live_files), roots=frozenset({"src.web2:entry"}))
    assert "src.web2:entry" in allowlist_problems(both, {"src.web2:entry": "x"})[1]


def test_control_an_unreferenced_function_is_reported_and_allowlists_are_checked(tmp_path):
    result = scan(
        _tree(tmp_path, {"src/pkg/mod.py": "def orphan(): pass\ndef used(): pass\nused()\n"})
    )
    assert result.dead == {"src.pkg.mod:orphan"}

    assert allowlist_problems(result, {"src.pkg.mod:orphan": "why"}) == ([], [])
    assert allowlist_problems(
        result,
        {"src.pkg.mod:orphan": "why", "src.pkg.mod:used": "live", "src.pkg.mod:gone": "x"},
    ) == ([], ["src.pkg.mod:gone", "src.pkg.mod:used"])


def test_control_module_function_reference_forms(tmp_path):
    result = scan(
        _tree(
            tmp_path,
            {
                "src/a.py": """
                    def by_name(): ...
                    def by_from_import(): ...
                    def by_alias(): ...
                    def by_dotted_import(): ...
                    def same_name_elsewhere(): ...
                    VALUE = by_name
                """,
                "src/b.py": """
                    import src.a
                    from src import a as mod_a
                    from src.a import by_from_import
                    mod_a.by_alias()
                    src.a.by_dotted_import()
                """,
                "src/c.py": "same_name_elsewhere()\n",
                "src/pkg/__init__.py": "",
                "src/pkg/x.py": "def by_relative(): ...\n",
                "src/pkg/y.py": "from .x import by_relative\n",
            },
        )
    )
    assert result.dead == {"src.a:same_name_elsewhere"}


def test_control_method_reference_forms(tmp_path):
    result = scan(
        _tree(
            tmp_path,
            {
                "src/m.py": """
                    class Client:
                        def by_attribute(self): ...
                        def by_async_prefix(self): ...
                        def by_getattr(self): ...
                        def by_template(self): ...
                        def unreferenced(self): ...

                    def drive(c):
                        c.by_attribute()
                        c.aby_async_prefix()
                        getattr(c, "by_getattr")()

                    drive(Client())
                """,
                "templates/admin/page.html": "<p>{{ client.by_template() }}</p>\n",
            },
        )
    )
    assert result.dead == {"src.m:Client.unreferenced"}


def test_control_fixpoint_dead_code_keeps_nothing_alive(tmp_path):
    result = scan(
        _tree(
            tmp_path,
            {
                "src/f.py": """
                    def root_caller(): leaf_of_live()
                    def leaf_of_live(): ...
                    def dead_caller(): leaf_of_dead()
                    def leaf_of_dead(): ...
                    def cycle_a(): cycle_b()
                    def cycle_b(): cycle_a()
                    def recursive(): recursive()

                    class K:
                        def dead_method(self): self.only_dead_calls_me()
                        def only_dead_calls_me(self): ...

                    root_caller()
                """,
            },
        )
    )
    assert result.dead == {
        "src.f:dead_caller",
        "src.f:leaf_of_dead",
        "src.f:cycle_a",
        "src.f:cycle_b",
        "src.f:recursive",
        "src.f:K.dead_method",
        "src.f:K.only_dead_calls_me",
    }


def test_control_skipped_and_tracked_definitions(tmp_path):
    result = scan(
        _tree(
            tmp_path,
            {
                "src/s.py": """
                    import functools
                    from typing import Protocol

                    def framework(fn): return fn

                    @framework
                    def decorated(): helper_of_decorated()

                    def helper_of_decorated(): ...

                    def __getattr__(name): ...

                    class Shape(Protocol):
                        def area(self) -> float: ...

                    class Thing:
                        def __init__(self): self.x = 1
                        def __repr__(self): return "t"
                        @staticmethod
                        def dead_static(): ...
                        @classmethod
                        def dead_class(cls): ...
                        @property
                        def dead_property(self): ...
                        @functools.cached_property
                        def dead_cached(self): ...
                """,
            },
        )
    )
    assert result.dead == {
        "src.s:Thing.dead_static",
        "src.s:Thing.dead_class",
        "src.s:Thing.dead_property",
        "src.s:Thing.dead_cached",
    }
    assert "src.s:Shape.area" not in result.definitions


def test_control_tests_and_dunder_all_do_not_count(tmp_path):
    result = scan(
        _tree(
            tmp_path,
            {
                "src/lib.py": """
                    __all__ = ["exported_only", "tested_only"]
                    def exported_only(): ...
                    def tested_only(): ...
                    def scripted(): ...
                    def migrated(): ...
                """,
                "tests/test_lib.py": "from src.lib import tested_only\ntested_only()\n",
                "scripts/tool.py": "from src.lib import scripted\nscripted()\n",
                "alembic/versions/0001_x.py": "from src.lib import migrated\n",
            },
        )
    )
    assert result.dead == {"src.lib:exported_only", "src.lib:tested_only"}


def test_control_known_limitation_shared_method_name_is_missed(tmp_path):
    # Documented false negative: Dead.close has no caller, but the name-based rule
    # credits it from Live().close(). If the scanner learns receiver types, update the
    # module docstring along with this expectation.
    result = scan(
        _tree(
            tmp_path,
            {
                "src/k.py": """
                    class Live:
                        def close(self): ...

                    class Dead:
                        def close(self): ...

                    Live().close()
                """,
            },
        )
    )
    assert result.dead == set()
