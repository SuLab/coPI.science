"""Live OriginGuard and exact canonical/legacy discussion-export contracts."""

from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from src.config import get_settings
from src.main import OriginGuardMiddleware, normalized_origin
from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER
from src.services import directory, profile_export
from tests import factories
from tests.integration.test_discussions_export_reviews import _world
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_workspace_permissions_matrix import (
    _database_hashes,
    _file_snapshot,
    _form,
    _target,
)
from tests.workspace_support import POST_SUFFIXES

pytestmark = pytest.mark.integration


def _assert_origin_refusal(response):
    assert response.status_code == 403
    assert response.text == 'Cross-site request refused.'


async def test_origin_guard_oracle_rejects_an_actual_new_route_bypass_mutant():
    """A newly registered unsafe route is protected; omitting middleware is caught."""
    for protected in (True, False):
        app = FastAPI()
        if protected:
            app.add_middleware(OriginGuardMiddleware)

        @app.post('/workspace/new-unsafe-registration')
        async def new_unsafe_registration():
            return {'executed': True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            response = await client.post('/workspace/new-unsafe-registration', headers={'Origin': 'https://attacker.invalid'})
        if protected:
            _assert_origin_refusal(response)
        else:
            assert response.status_code == 200 and response.json() == {'executed': True}
            with pytest.raises(AssertionError):
                _assert_origin_refusal(response)


async def test_origin_guard_refuses_every_workspace_write_before_auth_or_mutation(client_without_origin, db_session):
    pi, ids = await _target(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    before = await _database_hashes(db_session)
    persona_root = profile_export.PROFILES_DIR.parent
    files_before = _file_snapshot(persona_root)
    own = normalized_origin(get_settings().base_url)
    denied_headers = (
        {'Origin': 'https://attacker.invalid'}, {'Origin': 'null'}, {},
        {'Referer': 'https://attacker.invalid/form'}, {'Sec-Fetch-Site': 'cross-site'},
        {'Origin': 'null', 'Referer': own + '/workspace/pis'},
        {'Origin': 'https://attacker.invalid', 'Sec-Fetch-Site': 'same-origin'},
    )
    assert len(POST_SUFFIXES) == 28
    for prefix in ('/workspace', '/manager'):
        for suffix in sorted(POST_SUFFIXES):
            path = prefix + suffix.format(**ids)
            for headers in denied_headers:
                response = await client_without_origin.post(path, data=_form(),
                    headers={**auth_headers(manager.id), **headers})
                _assert_origin_refusal(response)
                assert await _database_hashes(db_session) == before, path
                assert _file_snapshot(persona_root) == files_before, path


async def test_origin_guard_precedence_allows_same_origin_safe_refusal(client_without_origin, db_session):
    pi, ids = await _target(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    own = normalized_origin(get_settings().base_url)
    before = await _database_hashes(db_session)
    for prefix in ('/workspace', '/manager'):
        path = f"{prefix}/pis/{pi.id}/candidates/{ids['candidate_id']}/accept"
        for headers in (
            {'Origin': own}, {'Sec-Fetch-Site': 'same-origin'}, {'Referer': own + '/workspace/pis'},
            {'Origin': 'null', 'Sec-Fetch-Site': 'same-origin'},
            {'Origin': own, 'Sec-Fetch-Site': 'cross-site', 'Referer': 'https://attacker.invalid'},
        ):
            response = await client_without_origin.post(path, data=_form(), headers={**auth_headers(manager.id), **headers})
            assert response.status_code == 404  # Real, already-resolved candidate; no effect.
            assert await _database_hashes(db_session) == before


@pytest.mark.parametrize('value', [None, '', 'true', 'html', 'false'])
@pytest.mark.parametrize('role', [None, USER_ROLE_PI, USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_discussion_export_truthiness_contract(client, db_session, role, value):
    admin, run = await _world(db_session)
    user = admin if role == USER_ROLE_ADMIN else (await factories.make_user(db_session, user_role=role) if role else None)
    pairs = [('run_id', str(run.id)), ('agent_filter', 'su'), ('agent_filter', 'lotz'), ('page', '1')]
    if value is not None:
        pairs.append(('export', value))
    query = '?' + urlencode(pairs)
    headers = auth_headers(user.id) if user else {}
    exporting = bool(value)
    for prefix in ('/workspace', '/admin', '/manager'):
        response = await client.get(prefix + '/discussions' + query, headers=headers, follow_redirects=False)
        if role is None:
            assert response.status_code == 302 and '/login' in response.headers['location']
        elif role in {USER_ROLE_PI, USER_ROLE_REVIEWER} or (prefix == '/admin' and role != USER_ROLE_ADMIN):
            assert response.status_code == 403
        elif prefix == '/workspace' and role == USER_ROLE_MANAGER and exporting:
            assert response.status_code == 403
        elif prefix == '/manager' or (prefix == '/admin' and not exporting):
            assert response.status_code == 302
            expected = [(key, val) for key, val in pairs if prefix != '/manager' or key != 'export']
            assert response.headers['location'] == '/workspace/discussions?' + urlencode(expected)
            location = urlsplit(response.headers['location'])
            assert not location.scheme and not location.netloc
            assert parse_qsl(location.query, keep_blank_values=True) == expected
            landing = await client.get(response.headers['location'], headers=headers, follow_redirects=False)
            assert landing.status_code == 200
            assert landing.headers['content-type'].startswith('text/html')
            assert 'content-disposition' not in landing.headers
            assert 'REVIEW-COMMENT-XYZ' not in landing.text
        else:
            assert response.status_code == 200
            if exporting:
                suffix = 'html' if value == 'html' else 'txt'
                assert response.headers['content-type'].startswith('text/html' if value == 'html' else 'text/plain')
                assert response.headers['content-disposition'] == f'attachment; filename=proposals.{suffix}'
                assert 'REVIEW-COMMENT-XYZ' in response.text and 'A joint assay platform.' in response.text
                assert '3/4' in response.text and '2026-08-01 09:30 UTC' in response.text
            else:
                assert response.headers['content-type'].startswith('text/html')
                assert 'content-disposition' not in response.headers
                assert 'REVIEW-COMMENT-XYZ' not in response.text


@pytest.mark.parametrize('value', ['true', 'html', 'false'])
async def test_discussion_exports_remain_uncapped_across_both_admin_routes(client, db_session, monkeypatch, value):
    monkeypatch.setattr(directory, 'DISCUSSIONS_PAGE_SIZE', 1)
    monkeypatch.setattr(directory, 'DISCUSSIONS_ALL_RUNS_MAX', 1)
    admin, run = await _world(db_session)
    for index in range(3):
        root = await factories.make_agent_message(db_session, run=run, agent_id='su', channel_name='general',
            phase='new_post', message_ts=f'1700000010.00010{index}', content='root')
        await factories.make_thread_decision(db_session, run=run, thread_id=root.message_ts, channel='general',
            agent_a='su', agent_b='lotz', outcome='proposal', summary_text=f'UNCAPPED-PROPOSAL-{index}')
    headers = auth_headers(admin.id)
    page = await client.get('/workspace/discussions?run_id=all', headers=headers)
    assert page.status_code == 200 and 'UNCAPPED-PROPOSAL-0' not in page.text
    for prefix in ('/workspace', '/admin'):
        response = await client.get(prefix + f'/discussions?run_id=all&page=2&export={value}', headers=headers)
        assert response.status_code == 200 and 'REVIEW-COMMENT-XYZ' in response.text
        assert all(f'UNCAPPED-PROPOSAL-{index}' in response.text for index in range(3))


@pytest.mark.parametrize('value', ['true', 'html', 'false'])
async def test_discussion_export_keeps_the_no_run_html_boundary(client, db_session, value):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    for prefix in ('/workspace', '/admin', '/manager'):
        response = await client.get(prefix + f'/discussions?export={value}', headers=auth_headers(admin.id))
        if prefix == '/manager':
            assert response.status_code == 302 and response.headers['location'] == '/workspace/discussions'
            response = await client.get(response.headers['location'], headers=auth_headers(admin.id))
        assert response.status_code == 200 and response.headers['content-type'].startswith('text/html')
        assert 'content-disposition' not in response.headers
        assert 'No simulation runs' in response.text and 'REVIEW-COMMENT-XYZ' not in response.text
