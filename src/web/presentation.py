"""Allowlisted page records. ORM objects never cross the workspace boundary.

Service read models remain unchanged for business and chat callers. This adapter
copies only display fields, recursively; adding a database column does not expose it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from src.services.verdict_fields import VERDICT_FIELDS


@dataclass(frozen=True)
class PageRecord(Mapping):
    _data: dict

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __getattr__(self, key):
        try:
            return self._data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


RESEARCH_USER = frozenset("id name user_role orcid institution department".split())
ACCOUNT_USER = frozenset(
    "email email_verified_at name_sanitized_at onboarding_complete claimed_at created_at last_login_at agent".split()
)
RESEARCH_PROFILE = frozenset(
    "id user_id profile_version research_summary techniques experimental_models disease_areas key_targets keywords".split()
)
ASSESSMENT = frozenset(
    "id simulation_run_id agent_id subject_agent_id channel_name created_at rubric_version rubric_content_hash panel_state review_cols dimension_rows revision_view panel_owed panel_received panel_failure_reason missing_domains prose_format".split()
) | frozenset(f.column for f in VERDICT_FIELDS if f.column)
SCHEMAS = {
    "User": RESEARCH_USER,
    "ResearcherProfile": RESEARCH_PROFILE,
    "OpportunityAssessment": ASSESSMENT,
    "AgentRegistry": frozenset(
        "id agent_id bot_name pi_name user_id status muted_at persona_export_failed_at requested_at role".split()
    ),
    "Publication": frozenset(
        "id pmid pmcid doi title abstract journal year methods_text provenance excluded_at".split()
    ),
    "PublicationCandidate": frozenset(
        "id pmid title journal year reason source created_at".split()
    ),
    "PiGrant": frozenset(
        "id title core_project_num activity_code first_fy last_fy is_subproject vetoed_at identity_evidence".split()
    ),
    "PiOrcidFunding": frozenset("id title funder_name start_year end_year vetoed_at".split()),
    "PiIndustryEvidence": frozenset(
        "id company_name kind company_class year pi_role source vetoed_at evidence".split()
    ),
    "PiIndustryScore": frozenset(
        "raw_sum evidence_count scorer_version tenure_start_used components".split()
    ),
    "PiCompany": frozenset(
        "id company_name pi_role origin reviewed_at created_at funding_usd funding_as_of source_url funding_source_url".split()
    ),
    "PiGrantIdentity": frozenset(
        "status accepted_profile_ids pinned_profile_ids none_confirmed evaluated_at candidates".split()
    ),
    "Job": frozenset(
        "id type status attempts max_attempts enqueued_at completed_at last_error".split()
    ),
    "SimulationRun": frozenset(
        "id started_at ended_at status total_api_calls total_messages config finalized_at".split()
    ),
    "AgentChannel": frozenset("channel_name channel_type created_by_agent archived_at".split()),
    "ThreadDecision": frozenset("id agent_a agent_b decided_at outcome summary_text".split()),
    "AssessmentReview": frozenset(
        "id assessment_id reviewer_user_id reviewer_name score comment feedback_mode edited consumed_at created_at updated_at dimension_scores rubric_version rubric_content_hash recorded_by_user_id recorded_by_name dimension_rows dimension_provenance".split()
    ),
    "AssessmentReviewEvent": frozenset(
        "id action actor_user_id actor_name recorded_by_user_id created_at".split()
    ),
    "AssessmentReviewAssignment": frozenset(
        "id assignee_user_id assignee_name assigned_by_user_id assigned_by_name created_at".split()
    ),
    "PromptChangeSuggestion": frozenset(
        "id assessment_id feedback_ids subject_label assessment_created_at rubric_version feedback_snapshot target prompt_files suggestion model transcript_available input_truncated raw_response status status_set_by_user_id status_set_by_name status_set_at created_at".split()
    ),
    "IndustryView": frozenset("reason percentile row coverage partial".split()),
    "ScopedCount": frozenset(
        "tenure_start in_tenure before_tenure undated_excluded scoped".split()
    ),
    "ScopedPublications": frozenset(
        "tenure_start publications before_tenure undated_excluded scoped excluded".split()
    ),
    "TenureScopedPublications": frozenset("publications tenure_start".split()),
    "ReviewCards": frozenset(
        "candidates unanchored excluded draft draft_stale draft_diff regenerate_refusal persona_out_of_date".split()
    ),
    "Draft": frozenset(
        "fields synthesis_validated evidence_pmid_count evidence_pub_count evidence_flagged_count base_profile_version job_id created_at".split()
    ),
    "RubricRevisionView": frozenset(
        "version content_hash scale_min scale_max advance_min conditional_min pass_label banding_note dimensions gating".split()
    ),
    "RevisionDimension": frozenset("key title weight weight_note".split()),
    "Banding": frozenset("advance_min conditional_min pass_label".split()),
    "RevisionRow": frozenset("created_at mechanism actor_name change_summary content".split()),
    "AgentThread": frozenset("id thread_id summary_claimed_at".split()),
    "GrantSections": frozenset("active past tenure_start".split()),
    "GrantLine": frozenset("source key title label start_year end_year".split()),
    "Suggestion": frozenset("text anchor kind section".split()),
    "ReviewColumns": frozenset("assigned_names reviewed_by_names status".split()),
}

# These fields contain intentionally open research/rubric JSON, not ORM objects.
JSON_FIELDS = frozenset(
    "techniques experimental_models disease_areas key_targets keywords scores gating dimension_rationales gating_rationales key_points dimension_scores identity_evidence evidence components coverage config fields content prompt_files feedback_snapshot analysis raw_verdict".split()
)
SECRET_KEYS = frozenset(
    "password password_hash slack_bot_token token_hash private_profile_seed private_profile_md user_submitted_texts pending_profile system_prompt messages_json".split()
)
STAFF_VERDICT_FIELDS = frozenset(f.column for f in VERDICT_FIELDS if f.staff_only)
STAFF_KEYS = frozenset(
    "jobs review revisions draft discovery company_suggestions companies_suggested activation_blockers activation_blocked activated blockers has_bot_token can_retry_profile profile_status agent_status email email_verified_at name_sanitized_at onboarding_complete claimed_at last_login_at".split()
)
ADMIN_KEYS = frozenset(
    "raw_verdict raw_opinion raw_opinion_truncated unplaced_turns held_counts in_doubt".split()
)
REVIEWER_SCHEMAS = {
    "PiGrantIdentity": SCHEMAS["PiGrantIdentity"] - {"candidates"},
    "IndustryView": frozenset({"percentile", "row"}),
    "PiIndustryScore": frozenset({"raw_sum", "evidence_count"}),
    "SimulationRun": frozenset({"id", "started_at", "ended_at", "status", "config"}),
}

# Fixed derived-view dictionary schema. Unknown future keys are not page data.
VIEW_FIELDS = frozenset(
    (
        "No Project _order _signals a abstract accepted_profile_ids accession action activated "
        "activation_blocked activation_blockers active activity_code actor actor_id actor_name "
        "actor_user_id additionalProperties admin admin_view advance_min advisor agent agent_a agent_b "
        "agent_filter agent_id agent_stats agent_status agents all all_runs_refused allow_historical "
        "analyses_per_press analysis anchor anchors approved_at approved_by archived_at as_of assessment "
        "assessment_counts_by_run assessment_created_at assessment_id assessments assessments_limit "
        "assigned_by_name assigned_by_user_id assigned_names assignee_name assignee_user_id assignment at attempts "
        "avg_length badge band_counts banding banding_note base_profile_version before_tenure betas "
        "billed block blockers board body bot_name bot_score bots can_retry_profile candidates change "
        "change_summary changed_by_user_id channel channel_filter channel_name channel_stats channel_type "
        "channels chat chat_enabled chat_suggestions chips citations claimed_at claimed_filter "
        "clear_funding co_founder comment companies_confirmed companies_suggested company_class "
        "company_id company_name company_role_labels company_roles company_suggestions completed_at "
        "components concern_count concerns conditional_min confidence config confirmed_companies "
        "constraint consult consult_count consults consumed_at content content_hash content_normalized "
        "context_tier core_project_num corresponding count counted counts coverage create created_at "
        "created_by_agent created_by_user_id current_hash cut_off date decided_at decision decision_id "
        "default default_factory deferred_until department dependencies description detail device_dx "
        "dimension dimension_provenance dimension_rationales dimension_rows dimension_scores "
        "dimension_stats dimensions discovery disease_areas distinct_companies doi doi_verified domain "
        "draft draft_diff draft_stale drop_counts drops_total edited effort eligible_count else_ email "
        "email_verified_at enabled encoding end_year ended_at enqueued_at entries error error_code "
        "established evaluated_at event evidence evidence_count evidence_flagged_count "
        "evidence_pmid_count evidence_pub_count exc_info excluded excluded_at exist_ok expected_version "
        "experimental_models fallbacks feature feedback_ids feedback_mode feedback_snapshot fields "
        "file_status filed filings finalized_at first first_fy for form form_d_status format former "
        "founder fragment frozen funder_name funding funding_as_of funding_note funding_source_url "
        "funding_usd fundings gating gating_definitions gating_descriptions gating_rationales "
        "gating_reasons ge grant_identity grant_sections grants has_bot_token has_older_row has_token "
        "headers held_counts hours id identity identity_evidence in in_doubt in_tenure inactive "
        "incomplete_panel_count index_elements industry industry_evidence initiated_by input_truncated "
        "institution institution_filter inventor is_consult is_hub is_subproject is_verdict_message items "
        "jhu_tenure_start job_id jobs journal key key_points key_targets keywords kind lab lab_filter "
        "lab_options label last last_error last_fy last_login_at latency_ms le links log_id "
        "log_scan_limit logs_scanned manager max max_attempts max_retries max_tokens maxsplit mean "
        "mechanism message message_total message_ts messages messages_available messages_json methods "
        "methods_text mid_scale mid_scale_count min minutes missing_domains missing_ok model "
        "most_active_agent most_active_count muted muted_at n name name_sanitized_at never new_role "
        "new_status no_result none_confirmed normalized_name note off_rubric_count onboarding_complete "
        "opinion orcid orcid_fundings origin other outcome output_config overall_official page page_count "
        "panel_by_thread panel_domains panel_failure_reason panel_owed panel_received panel_row_limit "
        "panel_state panel_summary panel_truncated parents partial pass_label password password_hash past "
        "path payload pct pending pending_profile pending_profile_created_at percentile "
        "persona_export_failed_at persona_out_of_date pharma_biotech phase pi pi_evidence pi_listed "
        "pi_name pi_role pi_user_id pi_user_ids pinned_profile_ids pmcid pmid populate_existing preview "
        "priority private_profile_md private_profile_seed profile profile_generated_at profile_status "
        "profile_version projection prompt_files prompt_sha256_12 properties proposals prose_format "
        "provenance pub_count pub_scope publications query question questions questions_to_ask rating "
        "rationale raw_opinion raw_opinion_truncated raw_response raw_sum raw_verdict read read_state "
        "reason record record_sha256_12 recorded_by_name recorded_by_user_id regenerate_refusal "
        "registered relationships replace_leftover replier reply_count reply_truncated request "
        "requested_at required research research_only research_summary response_class result_excerpt "
        "result_full result_truncated retro_consult_count reverse review review_assignments "
        "review_capable_users review_cols review_counts review_feedback review_rubric review_status "
        "review_status_history reviewed reviewed_at reviewed_by_names reviewed_by_user_id reviewer_id reviewer_name "
        "reviewer_user_id reviews revision revision_provenance revision_provenance_unknown revision_view "
        "revisions risk risks role roles row rubric_content_hash rubric_version run run_id runs "
        "runs_by_id scale_known scale_max scale_min schema scoped score scored_dimension_count "
        "scorer_version scores section selected_run_id sender_name served_by_model set_ show_all_runs "
        "shown_email signals simulation_run_id skip slack_bot_token sold sort sort_options source "
        "source_url specialist staff start_year started_at statements status status_code status_filter "
        "status_set_at status_set_by_name status_set_by_user_id stored_hash strength strengths strict "
        "subject_agent_id subject_label suggestion suggestions suggestions_limit summary "
        "summary_claimed_at summary_html summary_text synchronize_session synthesis_validated system "
        "system_prompt target target_user techniques tenure_provisional tenure_start tenure_start_used "
        "text thinking thread_id thread_total threads thresholds tier timeline timeout title today "
        "token_hash tool tool_turns total total_api_calls total_channels total_count total_length "
        "total_messages total_runs transcript_available type type_ tzinfo unanchored uncapped "
        "undated_excluded unestablished unplaced_turns unreviewed updated_at url usage_by_model user "
        "user_data user_id user_role user_submitted_texts user_total valid_user_roles value "
        "verdict_revision verdict_signal verdict_signals verified_email version vetoed_at viewer_is_staff "
        "weight weight_note wikidata worst year "
    ).split()
)
DYNAMIC_MAP_FIELDS = frozenset(
    "runs_by_id assessment_counts_by_run pi_user_ids panel_by_thread agent_stats channel_stats band_counts drop_counts review_counts counts gating_definitions gating_reasons gating_descriptions".split()
)


def _json(value):
    if value is None or isinstance(value, (str, int, float, bool, UUID, date, datetime, Decimal)):
        return value
    if isinstance(value, Mapping):
        return {k: _json(v) for k, v in value.items() if k not in SECRET_KEYS}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json(v) for v in value]
    raise TypeError(f"Non-JSON value in a page field: {type(value).__name__}")


def _fields(value, names, permissions):
    names = names - SECRET_KEYS
    if hasattr(type(value), "__mapper__"):
        names = names & value.__dict__.keys()  # Never issue implicit lazy queries while rendering.
    if not permissions.staff:
        names = names - STAFF_KEYS
        if type(value).__name__ == "OpportunityAssessment":
            names = names - STAFF_VERDICT_FIELDS
    if not permissions.admin:
        names = names - ADMIN_KEYS
    result = {}
    for name in sorted(names):
        if not hasattr(value, name):
            continue
        item = getattr(value, name)
        if name == "config" and type(value).__name__ == "SimulationRun" and not permissions.staff:
            # The run selector needs only a version label. Run configuration also
            # contains prompt stamps and operational announcement state.
            version = item.get("rubric_version") if isinstance(item, Mapping) else None
            item = {"rubric_version": version} if isinstance(version, str) else {}
        if (
            name == "evidence"
            and type(value).__name__ == "PiIndustryEvidence"
            and isinstance(item, Mapping)
        ):
            item = {k: item[k] for k in ("pi_mention", "span", "title") if k in item}
        if name == "identity_evidence" and isinstance(item, Mapping):
            item = {
                k: item[k]
                for k in ("matched_profile_id", "rule", "linking_pmids", "name_on_award")
                if k in item
            }
        json_field = (
            name in JSON_FIELDS
            or (name == "suggestion" and type(value).__name__ == "PromptChangeSuggestion")
            or (name == "candidates" and type(value).__name__ == "PiGrantIdentity")
        )
        result[name] = _json(item) if json_field else _project(item, permissions, name)
    return PageRecord(result)


def _project(value, permissions, field=""):
    if value is None or isinstance(value, (str, int, float, bool, UUID, date, datetime, Decimal)):
        return value
    if field in JSON_FIELDS:
        return _json(value)
    if isinstance(value, Mapping):
        return {
            key: _project(item, permissions, str(key))
            for key, item in value.items()
            if (field in DYNAMIC_MAP_FIELDS or str(key) in VIEW_FIELDS)
            and key not in SECRET_KEYS
            and (
                permissions.staff
                or key not in STAFF_KEYS
                or (key == "review" and (item is None or isinstance(item, str)))
            )
            and (permissions.admin or key not in ADMIN_KEYS)
        }
    if type(value).__name__ == "ReviewColumns":
        return _fields(value, SCHEMAS["ReviewColumns"], permissions)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_project(item, permissions, field) for item in value]
    if hasattr(value, "_mapping"):  # SQL column rows, not model instances.
        if field == "drop_counts":
            # This existing read model is a (reason, count) pair consumed by
            # the list renderer, not an object whose SQL labels are page keys.
            return tuple(_project(item, permissions) for item in value)
        return _project(dict(value._mapping), permissions)
    kind = type(value).__name__
    names = SCHEMAS.get(kind)
    if kind == "User" and permissions.staff and field in {"target_user", "user"}:
        names = names | ACCOUNT_USER
    if kind == "OpportunityAssessment" and permissions.admin:
        names = names | {"raw_verdict", "thread_id", "summary_claimed_at"}
    if not permissions.staff:
        names = REVIEWER_SCHEMAS.get(kind, names)
    if isinstance(value, SimpleNamespace):  # Explicit display-only test/read views.
        names = frozenset().union(*SCHEMAS.values()) | {
            "advance_min",
            "conditional_min",
            "pass_label",
        }
    if names is None:
        raise TypeError(f"No page projection registered for {kind}")
    return _fields(value, names, permissions)


def project_workspace(section, content, permissions):
    """Project already-authorized feature read models; never change chat inputs."""
    return _project(content, permissions)
