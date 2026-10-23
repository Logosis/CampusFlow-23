"""D-S11：真实事务、来源、当前权限、分页、回滚与 HTTP 契约。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from backend.d import audit as service, credentials, sessions
from backend.d import login as login_service
import backend.d.seed as seed_module
from backend.d.audit_schemas import AuditEventOutput
from backend.d.main import app
from backend.d.models import AuditEvent, Base, Role, RoleGrant, Space, SystemGrant, SystemSubject, User
from backend.shared.clock import format_time
from backend.shared.errors import ApiError

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
PATH = '/api/v1/d/audit'


def payload(**fields):
    return {'operation': 'APPEND', 'requestId': 'audit-request', 'sourceModule': 'M3',
            'actorType': 'USER', 'actorId': 'u-admin', 'objectType': 'WORK_ORDER', 'businessId': 'work-1',
            'spaceId': 'sp-cs', 'action': 'DISPATCH', 'occurredAt': format_time(START),
            'result': 'SUCCESS', 'summary': '派单完成', **fields}


def perform(data=None, *, peer='m3', mode='SYSTEM', user=None):
    return service.audit(input=payload() if data is None else data, source_module=peer, actor_mode=mode, user_id=user)


def get(**fields):
    return {'operation': 'GET', 'requestId': 'audit-request', **fields}


def listing(**fields):
    return {'operation': 'LIST', **fields}


def denied(code, action):
    with pytest.raises(ApiError) as caught:
        action()
    assert caught.value.code == code


def snapshot(factory):
    with factory() as db:
        return {t.name: db.execute(select(t).order_by(*t.primary_key.columns)).all() for t in Base.metadata.sorted_tables}


@pytest.fixture
def audit_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()
    def factory(module):
        assert module == 'd'
        return isolated_db
    for target in (service, sessions, login_service):
        monkeypatch.setattr(target, 'session_factory', factory)
    monkeypatch.setattr(credentials, 'ROOT', tmp_path)
    monkeypatch.setenv('D_SESSION_SECRET', 'ad' * 32)
    clock = tmp_path / 'clock.txt'
    clock.write_text(format_time(START), encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.setenv('TEST_CLOCK_FILE', str(clock))
    return isolated_db


@pytest.fixture
def client(audit_db, monkeypatch):
    monkeypatch.setattr(app.state, 'settings', replace(app.state.settings,
        credentials={peer: f'test-service-{peer}' for peer in ('d', 'm1', 'm2', 'm3')}))
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


@pytest.mark.parametrize('data,peer,mode,user,code', [
    (payload(), None, 'USER', 'u-admin', 'FORBIDDEN'),
    (payload(), 'd', 'SYSTEM', None, 'FORBIDDEN'),
    (payload(), 'm2', 'SYSTEM', None, 'FORBIDDEN'),
    (payload(), 'm3', 'USER', None, 'UNAUTHENTICATED'),
    (payload(), 'm3', 'USER', 'u-student1', 'FORBIDDEN'),
    (payload(actorType='SYSTEM', actorId='M3'), 'm3', 'USER', 'u-admin', 'FORBIDDEN'),
    (payload(actorType='UNKNOWN', actorId=None), 'm3', 'USER', 'u-admin', 'FORBIDDEN'),
    (payload(), 'm3', 'SYSTEM', 'u-admin', 'INVALID_ARGUMENT'),
    (get(), None, 'SYSTEM', None, 'FORBIDDEN'),
    (get(), None, 'USER', None, 'UNAUTHENTICATED'),
    (get(sourceModule='M2'), 'm3', 'SYSTEM', None, 'FORBIDDEN'),
    (listing(sourceModule='D'), 'm3', 'SYSTEM', None, 'FORBIDDEN'),
    (listing(), 'm3', 'invalid', None, 'INVALID_ARGUMENT'),
])
def test_untrusted_actor_never_opens_storage(monkeypatch, data, peer, mode, user, code):
    def fail(module):
        pytest.fail('Storage opened for untrusted actor')
    monkeypatch.setattr(service, 'session_factory', fail)
    denied(code, lambda: perform(data, peer=peer, mode=mode, user=user))


@pytest.mark.parametrize('error', [InterfaceError, OperationalError, SQLAlchemyTimeoutError])
@pytest.mark.parametrize('data', [payload(), get(), listing()])
def test_storage_failure_is_not_empty_success_and_hides_details(monkeypatch, error, data, caplog):
    def fail(module):
        raise error('private-password') if error is SQLAlchemyTimeoutError else error('private-sql', {}, Exception('private-password'))
    monkeypatch.setattr(service, 'session_factory', fail)
    denied('UNAVAILABLE', lambda: perform(data))
    assert 'D S11' in caplog.text and 'private' not in caplog.text


@pytest.mark.parametrize('actor,identifier', [('USER', 'u-admin'), ('SYSTEM', 'M3'), ('UNKNOWN', None)])
def test_append_get_exact_fields_and_seed_preservation(audit_db, actor, identifier):
    data = payload(actorType=actor, actorId=identifier)
    first = perform(data)
    assert first == {'auditId': first['auditId'], **{k: v for k, v in data.items() if k != 'operation'}, 'verifiedBy': 'M3'}
    assert AuditEventOutput.model_validate(first).model_dump() == first
    assert perform(data) == perform(get()) == first
    assert perform({'operation': 'GET', 'auditId': first['auditId']}) == first
    before = snapshot(audit_db)
    seed_module.seed()
    assert snapshot(audit_db) == before
    with audit_db() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert db.get(AuditEvent, first['auditId']).recorded_at == START


def test_user_verified_by_d_and_system_repeat_preserves_original(audit_db):
    first = perform(mode='USER', user='u-admin')
    assert first['verifiedBy'] == 'D'
    assert perform() == first
    other = perform(payload(requestId='system-first'))
    assert perform(payload(requestId='system-first'), mode='USER', user='u-admin') == other
    assert other['verifiedBy'] == 'M3'


def test_equivalent_timezone_is_same_event_not_new_input(audit_db):
    first = perform(payload(occurredAt='2026-01-01T08:00:00+08:00'))
    assert first['occurredAt'] == '2026-01-01T00:00:00Z'
    assert perform() == first


@pytest.mark.parametrize('changes', [
    {'summary': 'changed'}, {'actorId': 'u-student1'}, {'businessId': 'other'}, {'spaceId': None},
    {'action': 'CANCEL'}, {'objectType': 'OTHER'}, {'result': 'FAILED'}, {'occurredAt': '2026-01-01T00:00:01Z'},
])
def test_same_request_changed_content_rejected_without_writes(audit_db, changes):
    perform()
    before = snapshot(audit_db)
    denied('REQUEST_ID_REUSED', lambda: perform(payload(**changes)))
    assert snapshot(audit_db) == before


def test_source_namespaces_get_ambiguity_and_isolation(audit_db):
    first = perform()
    second = perform(payload(sourceModule='M2'), peer='m2')
    assert first['auditId'] != second['auditId']
    assert perform(get()) == first and perform(get(), peer='m2') == second
    denied('NOT_FOUND', lambda: perform({'operation': 'GET', 'auditId': first['auditId']}, peer='m2'))
    denied('CONFLICT', lambda: perform(get(), peer=None, mode='USER', user='u-admin'))
    assert perform(get(sourceModule='M2'), peer=None, mode='USER', user='u-admin') == second
    assert perform(listing())['items'] == [first]
    assert len(perform(listing(), peer=None, mode='USER', user='u-admin')['items']) == 2
    assert perform(listing(sourceModule='D'), peer=None, mode='USER', user='u-admin') == {'items': [], 'nextCursor': None}


@pytest.mark.parametrize('changes', [{'actorId': 'missing'}, {'spaceId': 'missing'}])
def test_unknown_references_leave_no_events(audit_db, changes):
    before = snapshot(audit_db)
    denied('NOT_FOUND', lambda: perform(payload(**changes)))
    assert snapshot(audit_db) == before


def test_history_can_reference_disabled_user_and_retired_space(audit_db):
    with audit_db.begin() as db:
        db.get(User, 'u-student1').account_status = 'DISABLED'
        db.get(Space, 'sp-cs').enabled = False
        db.get(Space, 'sp-cs').registered = False
    first = perform(payload(actorId='u-student1'))
    assert first['verifiedBy'] == 'M3' and perform(get()) == first
    denied('UNAUTHENTICATED', lambda: perform(payload(actorId='u-student1'), mode='USER', user='u-student1'))


def restrict(db, permission, kind, identifier, *, system=False):
    cls = SystemGrant if system else RoleGrant
    condition = cls.module_id == 'm3' if system else cls.role_id == 'r-admin'
    row = db.scalar(select(cls).where(condition, cls.permission_code == permission))
    row.scope_type, row.scope_id = kind, identifier


@pytest.mark.parametrize('system', [False, True])
@pytest.mark.parametrize('kind,identifier,space,allowed', [
    ('GLOBAL', None, None, True), ('DEPARTMENT', 'dep-cs', 'sp-cs', True),
    ('DEPARTMENT', 'dep-cs', 'sp-school', False), ('DEPARTMENT', 'dep-cs', None, False),
    ('SPACE', 'sp-cs', 'sp-cs', True), ('SPACE', 'sp-cs', 'sp-school', False), ('SPACE', 'sp-cs', None, False),
])
def test_append_and_read_enforce_current_scopes(audit_db, system, kind, identifier, space, allowed):
    data = payload(spaceId=space)
    first = perform(data)
    with audit_db.begin() as db:
        for code in ('d.audit.append', 'd.audit.read'):
            restrict(db, code, kind, identifier, system=system)
    kwargs = dict(mode='SYSTEM', user=None) if system else dict(mode='USER', user='u-admin')
    if allowed:
        assert perform(data, **kwargs) == perform(get(), **kwargs) == first
        assert perform(listing(), **kwargs)['items'] == [first]
    else:
        denied('FORBIDDEN', lambda: perform(data, **kwargs))
        denied('FORBIDDEN', lambda: perform(get(), **kwargs))
        assert perform(listing(), **kwargs)['items'] == []


def test_ambiguity_ignores_invisible_events_and_current_space_owner(audit_db):
    first = perform()
    perform(payload(sourceModule='M2', spaceId='sp-school'), peer='m2')
    with audit_db.begin() as db:
        restrict(db, 'd.audit.read', 'DEPARTMENT', 'dep-cs')
    args = dict(peer=None, mode='USER', user='u-admin')
    assert perform(get(), **args) == first
    with audit_db.begin() as db:
        db.get(Space, 'sp-cs').department_id = 'dep-ee'
    denied('FORBIDDEN', lambda: perform(get(), **args))
    assert perform(listing(), **args)['items'] == []


@pytest.mark.parametrize('system', [False, True])
def test_repeat_and_query_recheck_revoked_permissions(audit_db, system):
    perform()
    with audit_db.begin() as db:
        if system:
            db.get(SystemSubject, 'm3').status = 'DISABLED'
        else:
            db.get(Role, 'r-admin').status = 'DISABLED'
    args = dict(mode='SYSTEM', user=None) if system else dict(mode='USER', user='u-admin')
    for data in (payload(), get(), listing()):
        denied('FORBIDDEN', lambda: perform(data, **args))


def test_audit_actor_is_not_implicit_management_authority(audit_db):
    perform(payload(actorId='u-student1'), mode='USER', user='u-student1')
    denied('FORBIDDEN', lambda: perform(listing(), peer=None, mode='USER', user='u-student1'))
    denied('FORBIDDEN', lambda: perform(payload(actorId='u-screen'), mode='USER', user='u-screen'))


def test_filters_half_open_time_window_and_read_only(audit_db):
    rows = [perform(payload(requestId=f'e-{i}', occurredAt=f'2026-01-01T00:00:0{i}Z',
                            actorId='u-student1' if i < 2 else 'u-admin', action='START' if i < 2 else 'END')) for i in range(3)]
    before = snapshot(audit_db)
    assert perform(listing(actorUserId='u-student1', action='START', objectType='WORK_ORDER',
        businessId='work-1', spaceId='sp-cs', timeFrom='2026-01-01T08:00:00+08:00',
        timeTo='2026-01-01T00:00:02Z'))['items'] == rows[1::-1]
    assert perform(listing(timeFrom='2026-01-01T00:00:02Z'))['items'] == [rows[2]]
    assert perform(listing(businessId='missing'))['items'] == []
    denied('NOT_FOUND', lambda: perform(listing(actorUserId='missing')))
    denied('NOT_FOUND', lambda: perform(listing(spaceId='missing')))
    assert snapshot(audit_db) == before


def test_pagination_ties_filters_subject_scope_and_tamper(audit_db):
    events = [perform(payload(requestId=f'page-{i}')) for i in range(4)]
    expected = sorted(events, key=lambda row: row['auditId'], reverse=True)
    first = perform(listing(limit=2))
    assert first['items'] == expected[:2] and first['nextCursor']
    second = perform(listing(limit=2, cursor=first['nextCursor']))
    assert second == {'items': expected[2:], 'nextCursor': None}
    for change in ({'limit': 1}, {'action': 'DISPATCH'}, {'sourceModule': 'M3'}):
        denied('INVALID_ARGUMENT', lambda: perform(listing(limit=2, cursor=first['nextCursor']) | change))
    denied('INVALID_ARGUMENT', lambda: perform(listing(limit=2, cursor=first['nextCursor']), peer='m2'))
    denied('INVALID_ARGUMENT', lambda: perform(listing(limit=2, cursor=first['nextCursor']), mode='USER', user='u-admin'))
    denied('INVALID_ARGUMENT', lambda: perform(listing(limit=2, cursor=first['nextCursor'] + 'a')))
    with audit_db.begin() as db:
        restrict(db, 'd.audit.read', 'SPACE', 'sp-cs', system=True)
    denied('INVALID_ARGUMENT', lambda: perform(listing(limit=2, cursor=first['nextCursor'])))


def test_listing_explicit_out_of_scope_target_is_forbidden(audit_db):
    with audit_db.begin() as db:
        restrict(db, 'd.audit.read', 'SPACE', 'sp-cs', system=True)
    denied('FORBIDDEN', lambda: perform(listing(spaceId='sp-school')))


def test_failure_before_commit_rolls_back_and_uncertain_commit_reuses(audit_db, monkeypatch):
    class FailingSession(Session):
        pass
    def fail(session):
        raise OperationalError('commit', {}, Exception('private'))
    event.listen(FailingSession, 'before_commit', fail)
    factory = sessionmaker(bind=audit_db.kw['bind'], class_=FailingSession, expire_on_commit=False,
                          join_transaction_mode='create_savepoint')
    before = snapshot(audit_db)
    monkeypatch.setattr(service, 'session_factory', lambda module: factory)
    denied('UNAVAILABLE', perform)
    assert snapshot(audit_db) == before
    class UncertainFactory:
        @contextmanager
        def begin(self):
            with audit_db.begin() as db:
                yield db
            raise OperationalError('commit-response', {}, Exception('private'))
    monkeypatch.setattr(service, 'session_factory', lambda module: UncertainFactory())
    denied('UNAVAILABLE', perform)
    monkeypatch.setattr(service, 'session_factory', lambda module: audit_db)
    assert perform() == perform(get())
    with audit_db() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def headers(token=None, peer='m3', mode='SYSTEM'):
    value = {} if peer is None else {'X-Service-Credential': f'test-service-{peer}', 'X-Actor-Mode': mode}
    if token:
        value['Authorization'] = f'Bearer {token}'
    return value


def login(client, fixture_data, account='admin'):
    response = client.post('/api/v1/d/login', json={'account': account, 'password': fixture_data['password'],
                                                'requestId': f'audit-login-{account}'})
    assert response.status_code == 200
    return response.json()['data']['credential']


def test_http_contract_auth_health_and_no_private_fields(client, fixture_data, monkeypatch):
    admin = login(client, fixture_data)
    student = login(client, fixture_data, 'student1')
    response = client.post(PATH, json=payload(), headers=headers(admin, mode='USER'))
    assert response.status_code == 200
    first = response.json()
    assert first == {'ok': True, 'data': AuditEventOutput.model_validate(first['data']).model_dump(), 'error': None}
    assert first['data']['verifiedBy'] == 'D'
    assert client.post(PATH, json=payload(), headers=headers()).json() == first
    assert client.post(PATH, json=get(), headers=headers(admin, None)).json() == first
    assert client.post(PATH, json=listing(), headers=headers(admin, None)).json()['data']['items'] == [first['data']]
    health = client.get('/health').json()['data']
    assert health['unfinishedFunctions'] == [] and health['workerConfigured'] is True
    schema = client.get('/openapi.json').json()
    assert schema['paths'][PATH]['post']['requestBody']['content']['application/json']['schema']['discriminator']['propertyName'] == 'operation'
    for data, hdr, status, code in [
        (payload(), headers(admin, None), 403, 'FORBIDDEN'),
        (payload(), headers(peer='m2'), 403, 'FORBIDDEN'),
        (payload(), headers(student, mode='USER'), 403, 'FORBIDDEN'),
        (get(), headers(student, None), 403, 'FORBIDDEN'),
        (get(), headers('invalid', mode='USER'), 401, 'UNAUTHENTICATED'),
        (get(), headers(peer='m2'), 404, 'NOT_FOUND'),
        (get(), {}, 401, 'UNAUTHENTICATED'),
        (get(), headers(admin), 422, 'INVALID_ARGUMENT'),
        ({'operation': 'DELETE', 'auditId': first['data']['auditId']}, headers(), 422, 'INVALID_ARGUMENT'),
        ({'operation': 'GET', 'requestId': None}, headers(), 422, 'INVALID_ARGUMENT'),
        (payload(verifiedBy='D'), headers(), 422, 'INVALID_ARGUMENT'),
        (payload(summary='changed'), headers(), 409, 'REQUEST_ID_REUSED'),
    ]:
        result = client.post(PATH, json=data, headers=hdr)
        assert result.status_code == status
        assert result.json()['ok'] is False and result.json()['data'] is None and result.json()['error']['code'] == code
    assert all(key not in response.text for key in ('input_data', 'recorded_at', 'credential', 'password'))
    def fail(module):
        raise OperationalError('private-sql', {}, Exception('private-password'))
    monkeypatch.setattr(service, 'session_factory', fail)
    failed = client.post(PATH, json=listing(), headers=headers())
    assert failed.status_code == 503 and 'private' not in failed.text
