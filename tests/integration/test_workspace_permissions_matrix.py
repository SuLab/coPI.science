"""Live authorization and compatibility matrix for the consolidated workspace."""

import hashlib
import uuid
from datetime import UTC, datetime
from re import sub

import pytest
from sqlalchemy import func, inspect, select, text

from src.database import Base
from src.dependencies import get_review_user, get_staff_user
from src.models import (
    AccessAllowlist,
    AgentRegistry,
    Cohort,
    CohortMembership,
    Job,
    OpportunityAssessment,
    PiCompany,
    PiGrant,
    PiGrantIdentity,
    PiIndustryEvidence,
    PiOrcidFunding,
    PromptChangeSuggestion,
    Publication,
    PublicationCandidate,
    ResearcherProfile,
    SimulationRun,
    User,
)
from src.routers import admin, manager, workspace
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.workspace_support import POST_SUFFIXES

pytestmark = pytest.mark.integration


def _routes(router):
    for route in router.routes:
        context = getattr(route, "include_context", None)
        if context:
            yield from _routes(context.included_router)
        else:
            yield route


async def _target(db):
    pi = await factories.make_user(db, user_role="pi")
    await factories.make_profile(db, user=pi)
    await factories.make_agent(db, user=pi, status="active")
    candidate = PublicationCandidate(
        user_id=pi.id, pmid="991001", title="Candidate", reason="no_orcid_anchor", status="rejected"
    )
    publication = Publication(
        user_id=pi.id, pmid="991002", title="Publication", provenance="unanchored"
    )
    grant = PiGrant(
        user_id=pi.id, core_project_num="R01MATRIX", title="Grant", org_name="Test University",
        tenure_filter_mode="org_only",
    )
    identity = PiGrantIdentity(user_id=pi.id, candidates=[{"id": 111}])
    funding = PiOrcidFunding(user_id=pi.id, group_key="matrix", title="Funding")
    evidence = PiIndustryEvidence(
        user_id=pi.id, source="openalex", kind="coauthor_company", external_id="matrix",
        company_class="unknown", in_tenure=True,
    )
    company = PiCompany(
        user_id=pi.id, company_name="Matrix Co", normalized_name="matrix co", pi_role="founder",
        source_url="https://example.invalid/matrix", status="suggested", origin="discovered",
    )
    db.add_all((candidate, publication, grant, identity, funding, evidence, company))
    run = SimulationRun(status="completed", config={})
    db.add(run)
    await db.flush()
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="hub", subject_agent_id="lab", channel_name="matrix",
        company_or_project="Matrix assessment", recommendation="advance", band="advance", weighted_score=4,
    )
    suggestion = PromptChangeSuggestion(subject_label="Matrix suggestion", target="prompt", prompt_files=[],
                                        feedback_snapshot=[], suggestion="Matrix", transcript_available=False,
                                        status="open")
    db.add_all((assessment, suggestion))
    await db.flush()
    return pi, {
        "user_id": pi.id,
        "candidate_id": candidate.id, "publication_id": publication.id, "grant_id": grant.id,
        "funding_id": funding.id, "evidence_id": evidence.id, "company_id": company.id,
        "run_id": run.id, "assessment_id": assessment.id, "suggestion_id": suggestion.id,
        "agent_id": (await db.scalar(select(AgentRegistry.id).where(AgentRegistry.user_id == pi.id))),
    }


async def _snapshot(db, user_id):
    tables = (
        User, ResearcherProfile, AgentRegistry, Job, PublicationCandidate, Publication,
        PiGrant, PiGrantIdentity, PiOrcidFunding, PiIndustryEvidence, PiCompany,
    )
    counts = []
    for table in tables:
        column = User.id if table is User else table.user_id
        counts.append(await db.scalar(select(func.count()).select_from(table).where(column == user_id)))
    def normalize(value):
        if isinstance(value, datetime):
            return "set"
        if isinstance(value, (list, dict)):
            return str(value)
        if isinstance(value, str):
            return sub(r"(?:user|agent|Agent)\d+|Researcher \d+|0000-0000-0000-\d{4}", "<generated>", value)
        return "<id>" if isinstance(value, uuid.UUID) else value

    rows = []
    for table in tables:
        column = User.id if table is User else table.user_id
        names = [col.name for col in inspect(table).columns if "token" not in col.name and "password" not in col.name]
        values = (await db.execute(select(*[getattr(table, name) for name in names]).where(column == user_id))).all()
        rows.append(tuple(tuple(normalize(value) for value in row) for row in values))
    return tuple(counts), tuple(rows), await db.scalar(select(func.count()).select_from(Job))


async def _database_hashes(db):
    """One bounded query hashes every persisted column; secrets never leave PG."""
    quote = db.get_bind().dialect.identifier_preparer.quote
    statements = []
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        assert table.name.replace('_', '').isalnum()
        statements.append(
            f"SELECT '{table.name}' AS name, "
            "md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text, '[]')) AS digest "
            f"FROM {quote(table.name)} AS t"
        )
    return tuple((await db.execute(text(' UNION ALL '.join(statements)))).all())


async def test_state_oracle_detects_private_column_mutation_without_exposing_values(db_session):
    _pi, ids = await _target(db_session)
    before = await _database_hashes(db_session)
    agent = await db_session.get(AgentRegistry, ids['agent_id'])
    agent.slack_bot_token = 'OPAQUE_STATE_SENTINEL'
    await db_session.flush()
    after = await _database_hashes(db_session)
    assert before != after
    assert 'OPAQUE_STATE_SENTINEL' not in repr(after)


def _file_snapshot(root):
    if not root.exists():
        return ()
    return tuple(sorted(
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_file()
    ))


async def _effect(db, user_id, ids):
    """The target-owned columns each PI write can change, excluding acting-user attribution."""
    return (
        await db.scalar(select(ResearcherProfile.profile_version).where(ResearcherProfile.user_id == user_id)),
        await db.scalar(select(AgentRegistry.status).where(AgentRegistry.user_id == user_id)),
        await db.scalar(select(func.count()).select_from(Job).where(Job.user_id == user_id)),
        await db.scalar(select(func.count()).select_from(PiCompany).where(PiCompany.user_id == user_id)),
        await db.scalar(select(PublicationCandidate.status).where(PublicationCandidate.id == ids["candidate_id"])),
        (await db.execute(select(Publication.provenance, Publication.excluded_at.is_not(None)).where(Publication.id == ids["publication_id"]))).first(),
        await db.scalar(select(PiGrant.vetoed_at.is_not(None)).where(PiGrant.id == ids["grant_id"])),
        (await db.execute(select(PiGrantIdentity.status, PiGrantIdentity.pinned_profile_ids, PiGrantIdentity.none_confirmed).where(PiGrantIdentity.user_id == user_id))).first(),
        await db.scalar(select(PiOrcidFunding.vetoed_at.is_not(None)).where(PiOrcidFunding.id == ids["funding_id"])),
        await db.scalar(select(PiIndustryEvidence.vetoed_at.is_not(None)).where(PiIndustryEvidence.id == ids["evidence_id"])),
        await db.scalar(select(PiCompany.status).where(PiCompany.id == ids["company_id"])),
    )


def _form():
    return {
        "orcid": "0000-0001-0000-0011", "name": "Matrix Name", "email": "matrix@example.edu",
        "institution": "Test University", "department": "Matrix", "research_summary": "unchanged",
        "profile_version": "1", "profile_id_text": "", "company_name": "Matrix Added",
        "pi_role": "founder", "funding_usd": "", "funding_as_of": "", "source_url": "https://example.invalid",
    }


def _signature(response):
    return response.status_code, sub(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", "<id>", response.headers.get("location", ""))


_DOCUMENTED_REFUSALS = frozenset({
    "/pis/{user_id}/activate", "/pis/{user_id}/slack/provision",
    "/pis/{user_id}/candidates/{candidate_id}/accept", "/pis/{user_id}/candidates/{candidate_id}/reject",
})


@pytest.mark.parametrize("role,as_role", [
    (None, None), ("pi", None), ("reviewer", None), ("admin", "pi"), ("admin", "reviewer"),
], ids=["anonymous", "pi", "reviewer", "admin-as-pi", "admin-as-reviewer"])
async def test_all_28_writes_deny_untrusted_effective_identities(client, db_session, role, as_role, tmp_path):
    """Every alias receives a syntactically valid form and existing child identifiers."""
    target, ids = await _target(db_session)
    from src.services import profile_export

    agent_id = await db_session.scalar(select(AgentRegistry.agent_id).where(AgentRegistry.id == ids["agent_id"]))
    profile_export.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (profile_export.PROFILES_DIR / f"{agent_id}.md").write_text("persona sentinel")
    before = await _snapshot(db_session, target.id)
    persona_root = profile_export.PROFILES_DIR.parent
    assert persona_root.is_relative_to(tmp_path)
    files_before = _file_snapshot(persona_root)
    headers = {}
    if role:
        actor = await factories.make_user(db_session, user_role=role)
        effective = await factories.make_user(db_session, user_role=as_role) if as_role else None
        headers = auth_headers(actor.id, impersonate=effective.id if effective else None)
    values = {"user_id": target.id, **ids}
    for prefix in ("/workspace", "/manager"):
        for suffix in POST_SUFFIXES:
            response = await client.post(prefix + suffix.format(**values), data=_form(), headers=headers)
            if role is None:
                assert response.status_code == 302, (prefix + suffix, response.status_code)
                assert "/login" in response.headers["location"]
            else:
                assert response.status_code == 403, (prefix + suffix, response.status_code)
            assert await _snapshot(db_session, target.id) == before
            assert _file_snapshot(persona_root) == files_before


async def test_each_write_has_matching_manager_admin_effect_or_refusal(client, db_session):
    """Equivalent staff callers must receive the same concrete target-state outcome.

    Fresh target graphs make every action independent; resolved candidates exercise the
    meaningful already-decided refusal without invoking PubMed.
    """
    manager_user = await factories.make_user(db_session, user_role="manager")
    admin_user = await factories.make_user(db_session, user_role="admin")
    manager_headers = auth_headers(manager_user.id)
    admin_headers = auth_headers(admin_user.id)
    for suffix in sorted(POST_SUFFIXES):
        manager_target, manager_ids = await _target(db_session)
        admin_target, admin_ids = await _target(db_session)
        manager_target_id = manager_target.id
        admin_target_id = admin_target.id
        manager_values = {"user_id": manager_target.id, **manager_ids}
        admin_values = {"user_id": admin_target.id, **admin_ids}
        manager_form = {**_form(), "orcid": manager_target.orcid}
        admin_form = {**_form(), "orcid": admin_target.orcid}
        manager_form["email"] = f"{manager_target_id.hex}@example.invalid"
        admin_form["email"] = f"{admin_target_id.hex}@example.invalid"
        if suffix.endswith("/publications/{publication_id}/restore"):
            manager_publication = await db_session.get(Publication, manager_ids["publication_id"])
            admin_publication = await db_session.get(Publication, admin_ids["publication_id"])
            manager_publication.excluded_at = datetime.now(UTC)
            admin_publication.excluded_at = datetime.now(UTC)
        await db_session.commit()
        manager_response = await client.post(
            "/workspace" + suffix.format(**manager_values), data=manager_form,
            headers=manager_headers, follow_redirects=False,
        )
        manager_effect = await _effect(db_session, manager_target_id, manager_ids)
        admin_response = await client.post(
            "/workspace" + suffix.format(**admin_values), data=admin_form,
            headers=admin_headers, follow_redirects=False,
        )
        admin_effect = await _effect(db_session, admin_target_id, admin_ids)
        expected = 404 if suffix in _DOCUMENTED_REFUSALS else 302
        assert manager_response.status_code == admin_response.status_code == expected, suffix
        assert _signature(manager_response) == _signature(admin_response), suffix
        assert manager_effect == admin_effect, suffix


def test_canonical_and_legacy_post_inventory_is_exact_and_callable_identical():
    canonical = {route.path: route for route in _routes(workspace.router) if "POST" in getattr(route, "methods", ())}
    legacy = {route.path: route for route in _routes(manager.router) if "POST" in getattr(route, "methods", ())}
    _assert_write_inventory(canonical, legacy)


def _assert_write_inventory(canonical, legacy):
    assert set(canonical) == POST_SUFFIXES == set(legacy)
    for path in POST_SUFFIXES:
        assert canonical[path].endpoint is legacy[path].endpoint


def _assert_staff_dependencies(routes):
    for route in routes.values():
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert calls.count(get_staff_user) == 1


def test_write_dependency_oracle_rejects_removed_or_wrong_staff_guard():
    canonical = {route.path: route for route in _routes(workspace.router) if "POST" in getattr(route, "methods", ())}
    _assert_staff_dependencies(canonical)
    route = next(iter(canonical.values()))
    original = route.dependant.dependencies
    try:
        route.dependant.dependencies = [dependency for dependency in original if dependency.call is not get_staff_user]
        with pytest.raises(AssertionError):
            _assert_staff_dependencies(canonical)
    finally:
        route.dependant.dependencies = original


async def test_live_write_denial_oracle_rejects_a_wrong_dependency(client, db_session, asgi_app):
    """An actual reviewer request passes a deliberately substituted weaker guard."""
    pi, ids = await _target(db_session)
    reviewer = await factories.make_user(db_session, user_role='reviewer')
    path = f"/workspace/pis/{pi.id}/candidates/{ids['candidate_id']}/accept"
    headers = auth_headers(reviewer.id)
    assert (await client.post(path, headers=headers)).status_code == 403
    original = dict(asgi_app.dependency_overrides)
    before = await _database_hashes(db_session)
    try:
        asgi_app.dependency_overrides[get_staff_user] = get_review_user
        mutant = await client.post(path, headers=headers)
        assert mutant.status_code == 404  # Existing resolved candidate; no mutation.
        with pytest.raises(AssertionError):
            assert mutant.status_code == 403
        assert await _database_hashes(db_session) == before
    finally:
        asgi_app.dependency_overrides.clear()
        asgi_app.dependency_overrides.update(original)
    assert (await client.post(path, headers=headers)).status_code == 403


def test_write_inventory_rejects_a_new_unpaired_route():
    canonical = {route.path: route for route in _routes(workspace.router) if "POST" in getattr(route, "methods", ())}
    legacy = {route.path: route for route in _routes(manager.router) if "POST" in getattr(route, "methods", ())}
    canonical["/pis/mutant"] = next(iter(canonical.values()))
    with pytest.raises(AssertionError):
        _assert_write_inventory(canonical, legacy)


async def test_manager_gets_are_guarded_before_legacy_redirects(client, db_session):
    pi, _ids = await _target(db_session)
    staff = await factories.make_user(db_session, user_role="manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    canonical = ("/pis", f"/pis/{pi.id}", "/assessments", "/slack-bots", "/discussions", "/activity")
    for suffix in canonical:
        response = await client.get("/workspace" + suffix, headers=auth_headers(staff.id), follow_redirects=False)
        assert response.status_code == 200, suffix
    for suffix in ("/discussions", "/activity", "/slack-bots"):
        response = await client.get("/manager" + suffix, headers=auth_headers(reviewer.id), follow_redirects=False)
        assert response.status_code == 403, suffix
    legacy = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(staff.id), follow_redirects=False)
    assert legacy.status_code == 302
    assert legacy.headers["location"] == f"/workspace/pis/{pi.id}"
    invalid = await client.get(f"/manager/pis/{uuid.uuid4()}", headers=auth_headers(staff.id), follow_redirects=False)
    assert invalid.status_code == 404


@pytest.mark.parametrize("role,as_role", [
    (None, None), ("pi", None), ("reviewer", None), ("manager", None), ("admin", None),
    ("admin", "admin"), ("admin", "pi"), ("admin", "reviewer"), ("admin", "manager"),
], ids=["anonymous", "pi", "reviewer", "manager", "admin", "admin-as-admin", "admin-as-pi", "admin-as-reviewer", "admin-as-manager"])
async def test_all_workspace_get_and_head_routes_follow_effective_role(client, db_session, role, as_role):
    pi, ids = await _target(db_session)
    paths = (
        "/workspace", "/workspace/pis", f"/workspace/pis/{pi.id}",
        "/workspace/assessments", f"/workspace/assessments/{ids['assessment_id']}",
        "/workspace/slack-bots", "/workspace/discussions", "/workspace/activity",
        f"/workspace/activity/{ids['run_id']}", "/workspace/prompt-suggestions",
        f"/workspace/prompt-suggestions/{ids['suggestion_id']}",
    )
    headers = {}
    effective_role = role
    if role:
        actor = await factories.make_user(db_session, user_role=role)
        effective = await factories.make_user(db_session, user_role=as_role) if as_role else None
        headers = auth_headers(actor.id, impersonate=effective.id if effective else None)
        effective_role = as_role or role
    for path in paths:
        response = await client.get(path, headers=headers, follow_redirects=False)
        if effective_role is None:
            assert response.status_code == 302, path
        elif effective_role == "pi":
            assert response.status_code == 403, path
        elif effective_role == "reviewer":
            expected = 200 if any(part in path for part in ("/pis", "/assessments")) else 403
            if path == "/workspace":
                expected = 302
            assert response.status_code == expected, path
        else:
            assert response.status_code in {200, 302}, path
        head = await client.head(path, headers=headers, follow_redirects=False)
        assert head.status_code == response.status_code, path
        legacy = await client.get(path.replace("/workspace", "/manager", 1), headers=headers, follow_redirects=False)
        legacy_head = await client.head(path.replace("/workspace", "/manager", 1), headers=headers, follow_redirects=False)
        assert legacy_head.status_code == legacy.status_code, path
        if effective_role in {None, "pi"}:
            assert legacy.status_code == response.status_code, path
        elif effective_role == "reviewer" and path != "/workspace" and not any(part in path for part in ("/pis", "/assessments")):
            assert legacy.status_code == 403, path
        else:
            assert legacy.status_code == 302, path


def _admin_path(path, ids):
    values = {key: str(value) for key, value in ids.items()}
    return "/admin" + path.format(**values)


@pytest.mark.parametrize("role,as_role", [
    (None, None), ("pi", None), ("reviewer", None), ("manager", None), ("admin", "pi"), ("admin", "reviewer"),
    ("admin", "manager"),
], ids=["anonymous", "pi", "reviewer", "manager", "admin-as-pi", "admin-as-reviewer", "admin-as-manager"])
async def test_all_admin_methods_deny_nonadmin_effective_identity(client, db_session, role, as_role):
    pi, ids = await _target(db_session)
    agent = await db_session.get(AgentRegistry, ids['agent_id'])
    cohort = Cohort(name='Protected matrix cohort')
    entry = AccessAllowlist(orcid=pi.orcid, email=pi.email)
    db_session.add_all((cohort, entry))
    call = await factories.make_llm_call_log(db_session, simulation_run_id=ids['run_id'])
    db_session.add(CohortMembership(cohort_id=cohort.id, agent_id=agent.agent_id))
    await db_session.flush()
    ids.update(entry_id=entry.id, cohort_id=cohort.id, call_id=call.id)
    headers = {}
    if role:
        actor = await factories.make_user(db_session, user_role=role)
        effective = await factories.make_user(db_session, user_role=as_role) if as_role else None
        headers = auth_headers(actor.id, impersonate=effective.id if effective else None)
    routes = list(_routes(admin.router))
    assert len(routes) == 45
    from src.services import profile_export

    persona_root = profile_export.PROFILES_DIR.parent
    profile_export.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (profile_export.PROFILES_DIR / f'{agent.agent_id}.md').write_text('Protected persona')
    before = await _database_hashes(db_session)
    files_before = _file_snapshot(persona_root)
    for route in routes:
        method = next(method for method in route.methods if method not in {"HEAD", "OPTIONS"})
        path = _admin_path(route.path, ids)
        body = {
            "orcid": pi.orcid, "email": pi.email, "role": agent.role, "agent_status": "active",
            "bot_name": "Denied Bot", "name": "Denied cohort", "user_role": "reviewer",
            "user_id": str(pi.id), "agent_id": agent.agent_id, "agent_slug": agent.agent_id,
            "run_id": str(ids['run_id']), "confirm_run": str(ids['run_id']),
            "fresh": "false", "max_runtime": "0", "max_proposals": "0", "reset": "true",
            "present_agent": agent.agent_id, "present_cohort": str(cohort.id),
            "cell": f'{cohort.id}:{agent.agent_id}', "was_checked": f'{cohort.id}:{agent.agent_id}',
        }
        assert all(param.alias in body or not param.field_info.is_required() for param in route.dependant.body_params), route.path
        response = await client.request(method, path, data=body, headers=headers, follow_redirects=False)
        if path == "/admin/agents/slack/callback":
            assert response.status_code != 200
        elif role is None:
            assert response.status_code == 302
            assert "/login" in response.headers["location"]
        elif path == "/admin/impersonate/stop":
            assert response.status_code == 302
        else:
            assert response.status_code == 403, (method, path, response.status_code)
        if path not in {'/admin/agents/slack/callback', '/admin/impersonate/stop'}:
            assert await _database_hashes(db_session) == before, path
            assert _file_snapshot(persona_root) == files_before, path


def test_admin_route_inventory_has_no_unregistered_methods():
    routes = list(_routes(admin.router))
    assert len(routes) == 45
    methods = {method for route in routes for method in getattr(route, "methods", ())
               if method not in {"HEAD", "OPTIONS"}}
    assert methods == {"GET", "POST"}
