"""Real PostgreSQL in a disposable M3 schema; only D/M1 HTTP responses are simulated."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from backend.m3 import domain, peers
from backend.m3.models import Base
from backend.shared import client as shared_client, clock
from backend.shared.errors import STATUS
from backend.shared.settings import Settings

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/bootstrap.json').read_text())


class PeerDouble:
    def __init__(self):
        self.calls = []
        self.fail = {}
        self.responses = {}
        self.notifications = {}
        self.audits = {}
        self.limits = {}
        self.requests = {}
        self.users = {u['account']: deepcopy(u) for u in FIXTURE['users']}
        self.grants = {}
        for token, u in self.users.items():
            self.grants[token] = [dict(permissionCode=c, scope=role['scope'])
                for role in FIXTURE['roles'].values() if role['roleId'] in u['roleIds']
                for c in role['permissionCodes']]
        self.spaces = deepcopy(FIXTURE['spaces'])

    def grant(self, token, code, scope=None):
        self.grants[token].append({'permissionCode': code, 'scope': scope or {'type': 'GLOBAL', 'id': None}})

    def allowed(self, token, code, target=None):
        scopes = [g['scope'] for g in self.grants[token] if g['permissionCode'] == code]
        if target is None:
            return scopes
        space = next((s for s in self.spaces if s['spaceId'] == target['id']), None)
        return [s for s in scopes if s['type'] == 'GLOBAL' or s == target
                or s['type'] == 'DEPARTMENT' and space and s['id'] == space['departmentId']]

    def handler(self, request):
        target, fn = request.url.path.split('/')[-2:]
        body = json.loads(request.content)
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        mode = request.headers['X-Actor-Mode']
        assert request.headers['X-Service-Credential'] == Settings.load('m3').credentials['m3']
        assert mode == ('USER' if token else 'SYSTEM')
        self.calls.append((target, fn, deepcopy(body), mode, token))
        if (target, fn) in self.fail:
            code = self.fail[(target, fn)]
            if code == 'TRANSPORT':
                raise httpx.ReadTimeout('test lost response', request=request)
            return self.error(code)
        if (target, fn) in self.responses:
            value = self.responses[(target, fn)]
            return self.ok(value(body) if callable(value) else value)
        if target == 'd':
            if fn == 'get_identity':
                if token not in self.users or self.users[token]['accountStatus'] != 'ACTIVE':
                    return self.error('UNAUTHENTICATED')
                u = self.users[token]
                return self.ok({k: u[k] for k in ('userId', 'displayName', 'departmentId', 'accountStatus', 'roleIds')}
                    | {'grants': self.grants[token]})
            if fn == 'check_permission':
                scopes = self.allowed(token, body['permissionCode'], body.get('targetScope')) if token else [{'type': 'GLOBAL', 'id': None}]
                return self.ok(dict(subjectType=mode, subjectId=self.users[token]['userId'] if token else 'm3',
                    allowed=bool(scopes), allowedScopes=scopes, reason='test'))
            if fn == 'query_master':
                if body['operation'] == 'GET_SPACE':
                    item = next((s for s in self.spaces if s['spaceId'] == body['spaceId']), None)
                    return self.ok(item) if item else self.error('NOT_FOUND')
                rows = [s for s in self.spaces if not body.get('departmentId') or s['departmentId'] == body['departmentId']]
                return self.ok({'items': rows, 'nextCursor': None})
            if fn == 'query_people':
                codes = body['permissionCodes']
                if not set(codes).issubset(FIXTURE['permissions']):
                    return self.error('INVALID_ARGUMENT')
                rows = []
                ids = body.get('userIds', [u['userId'] for u in self.users.values()])
                for ident in ids:
                    account = next((t for t, u in self.users.items() if u['userId'] == ident), None)
                    if account is None:
                        return self.error('NOT_FOUND')
                    u = self.users[account]
                    eligible = u['accountStatus'] == 'ACTIVE' and all(self.allowed(account, c, body['targetScope']) for c in codes)
                    if body['mode'] == 'CANDIDATES' and not eligible:
                        continue
                    rows.append({k: u[k] for k in ('userId', 'displayName', 'departmentId', 'accountStatus')}
                        | dict(eligible=eligible, matchedPermissionCodes=codes if eligible else []))
                return self.ok({'people': rows} if body['mode'] == 'BY_IDS' else {'items': rows, 'nextCursor': None})
            if fn == 'send_notification':
                rid = body['requestId']
                self.notifications.setdefault(rid, body | dict(notificationId='n-' + rid, channel='EMAIL',
                    createdAt=clock.format_time(clock.utc_now()), deliveries=[dict(recipientUserId=u,
                    status='SUBMITTED', attemptCount=1, lastAttemptAt=clock.format_time(clock.utc_now()), reason='')
                    for u in body['recipientUserIds']]))
                return self.ok(self.notifications[rid])
            if fn == 'get_notification':
                return self.ok(self.notifications[body['requestId']]) if body['requestId'] in self.notifications else self.error('NOT_FOUND')
            if fn == 'audit':
                if body['operation'] == 'APPEND':
                    self.audits.setdefault(body['requestId'], body | {'auditId': 'a-' + body['requestId'], 'verifiedBy': 'M3'})
                return self.ok(self.audits[body['requestId']]) if body['requestId'] in self.audits else self.error('NOT_FOUND')
        if target == 'm1':
            bid, rid = body['businessId'], body.get('requestId')
            if fn == 'change_room_limit':
                old = self.limits.get(bid)
                if old and old['sourceVersion'] > body['sourceVersion']:
                    self.requests[rid] = 'REJECTED'
                    return self.error('CONFLICT')
                self.limits[bid] = {k: body[k] for k in ('source', 'businessId', 'spaceId', 'startTime', 'endTime', 'sourceVersion')} | {'state': 'ACTIVE' if body['action'] == 'REPAIR' else 'RELEASED'}
                self.requests[rid] = 'SUCCESS'
            limit = self.limits.get(bid)
            return self.ok(dict(source='M3', businessId=bid, spaceId=limit['spaceId'] if limit else None,
                maxProcessedVersion=limit['sourceVersion'] if limit else None, currentLimit=limit,
                queriedRequestId=rid, requestStatus=self.requests.get(rid, 'UNKNOWN') if rid else None, message='test'))
        raise AssertionError(f'unexpected peer {target}.{fn}')

    @staticmethod
    def ok(data):
        return httpx.Response(200, json={'ok': True, 'data': data, 'error': None})

    @staticmethod
    def error(code):
        return httpx.Response(STATUS[code], json={'ok': False, 'data': None, 'error': {'code': code, 'message': code}})


@pytest.fixture
def db_factory(monkeypatch):
    settings = Settings.load('m3')
    engine = create_engine(settings.database_url)
    schema = 'm3_test_' + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    isolated = create_engine(settings.database_url, connect_args={'options': f'-csearch_path={schema}'})
    Base.metadata.create_all(isolated)
    factory = sessionmaker(bind=isolated, expire_on_commit=False)
    monkeypatch.setattr(domain, 'sessions', lambda: factory)
    yield factory
    isolated.dispose()
    with engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    engine.dispose()


@pytest.fixture
def peers_double(monkeypatch):
    fake = PeerDouble()
    original = httpx.AsyncClient
    def client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(fake.handler)
        return original(*args, **kwargs)
    monkeypatch.setattr(shared_client.httpx, 'AsyncClient', client)
    return fake


@pytest.fixture
def now(monkeypatch):
    value = [datetime(2026, 10, 15, 4, tzinfo=timezone.utc)]
    monkeypatch.setattr(clock, 'utc_now', lambda: value[0])
    monkeypatch.setattr(domain, 'utc_now', lambda: value[0])
    from backend.m3 import service
    monkeypatch.setattr(service, 'utc_now', lambda: value[0])
    return value


@pytest.fixture
def api(db_factory, peers_double, now):
    from backend.m3.api import router
    from backend.shared.factory import create_app
    app = create_app('m3', router)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


@pytest.fixture
def call(api):
    def invoke(fn, body=None, token='student1', status=200, source=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        if source:
            headers |= {'X-Service-Credential': Settings.load('m3').credentials[source], 'X-Actor-Mode': 'USER' if token else 'SYSTEM'}
        response = api.post('/api/v1/m3/' + fn, json=body or {}, headers=headers)
        assert response.status_code == status, response.text
        result = response.json()
        assert set(result) == {'ok', 'data', 'error'}
        return result['data'] if status == 200 else result['error']
    return invoke
