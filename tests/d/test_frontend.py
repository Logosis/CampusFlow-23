"""D local static entry points; no database or fake authentication is required."""
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from backend.d.main import app
from backend.d import frontend


@pytest.mark.parametrize('url,content_type,marker', [
    ('/', 'text/html', '校园服务入口'),
    ('/index.html', 'text/html', 'id="workspace" hidden'),
    ('/login.html', 'text/html', 'autocomplete="current-password"'),
    ('/shared/auth.js', 'text/javascript', 'export async function api'),
    ('/shared/d-session.js', 'text/javascript', 'export async function logout'),
    ('/shared/d-pages.js', 'text/javascript', 'textContent'),
    ('/shared/d.css', 'text/css', '@media (max-width: 760px)'),
])
def test_local_page_and_asset_responses(url, content_type, marker):
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith(content_type)
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert marker in response.text
        head = client.head(url)
        assert head.status_code == 200 and head.content == b''


@pytest.mark.parametrize('url', [
    '/.env', '/backend/d/models.py', '/shared/../../.env',
    '/shared/%2e%2e/%2e%2e/.env', '/tests/fixtures/bootstrap.json', '/m1/',
])
def test_static_entry_does_not_expose_repo_or_claim_other_module_pages(url):
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'NOT_FOUND'


def test_query_parameters_cannot_choose_another_file():
    with TestClient(app) as client:
        response = client.get('/login.html', params={'relative': '.env', 'path': '.env'})
        assert response.status_code == 200
        assert '<form id="login-form" hidden>' in response.text


def test_missing_static_bundle_in_backend_container_is_404_not_startup_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(frontend, 'FRONTEND_ROOT', tmp_path)
    with TestClient(app) as client:
        assert client.get('/login.html').status_code == 404
        assert client.get('/health').json()['data']['unfinishedFunctions'] == []


def test_frontend_keeps_existing_api_guard_and_public_function_registry():
    with TestClient(app) as client:
        assert client.get('/health').json()['data']['unfinishedFunctions'] == []
        response = client.post('/api/v1/d/get_identity', json={})
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'UNAUTHENTICATED'
        schema = client.get('/openapi.json').json()
        assert '/login.html' not in schema['paths']
        assert len([p for p in schema['paths'] if p.startswith('/api/v1/d/')]) == 12


def test_assets_use_relative_same_origin_imports_and_no_inline_secrets():
    root = frontend.FRONTEND_ROOT
    for name in ('index.html', 'login.html'):
        html = (root / name).read_text(encoding='utf-8')
        assert '<html lang="zh-CN">' in html
        assert '<script type="module" src="/shared/d-pages.js"></script>' in html
        assert 'target="_blank"' not in html  # same tab keeps sessionStorage credentials
    home = (root / 'index.html').read_text(encoding='utf-8')
    for module in ('m1', 'm2', 'm3'):
        assert f'href="/{module}/"' in home
    for asset in (root / 'shared').glob('*.js'):
        code = asset.read_text(encoding='utf-8')
        assert 'localStorage' not in code
        assert 'X-Service-Credential' not in code
        assert 'innerHTML' not in code
