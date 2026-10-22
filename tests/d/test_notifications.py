"""S09/S10：真实持久化、权限、重复、回滚及完整 HTTP 返回。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from backend.d import credentials, notifications, sessions
from backend.d import login as login_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.models import (
    Base, Notification, NotificationDelivery, Permission, PublicConfig,
    Role, RoleGrant, Space, SystemGrant, SystemSubject, User,
)
from backend.d.notification_schemas import NotificationOutput
from backend.shared.clock import format_time
from backend.shared.errors import ApiError

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
SEND = '/api/v1/d/send_notification'
GET = '/api/v1/d/get_notification'


def payload(**fields):
    return {'requestId': 'notice-request', 'sourceModule': 'M3', 'businessId': 'work-order-1',
            'spaceId': 'sp-cs', 'recipientUserIds': ['u-student1', 'u-student2'],
            'kind': 'M3_ASSIGNED', 'title': '维修通知', 'body': '第一行\n第二行', **fields}


def send(data=None, *, peer='m3', actor='SYSTEM', user=None):
    return notifications.send_notification(input=payload() if data is None else data,
        source_module=peer, actor_mode=actor, user_id=user)


def get(data=None, *, peer='m3', actor='SYSTEM', user=None):
    return notifications.get_notification(input={'requestId': 'notice-request'} if data is None else data,
        source_module=peer, actor_mode=actor, user_id=user)


def denied(code, action):
    with pytest.raises(ApiError) as caught:
        action()
    assert caught.value.code == code


def snapshot(factory):
    with factory() as db:
        return {t.name: db.execute(select(t).order_by(*t.primary_key.columns)).all()
                for t in Base.metadata.sorted_tables}


@pytest.fixture
def notice_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()
    def factory(module):
        assert module == 'd'
        return isolated_db
    for service in (notifications, sessions, login_service):
        monkeypatch.setattr(service, 'session_factory', factory)
    monkeypatch.setattr(credentials, 'ROOT', tmp_path)
    monkeypatch.setenv('D_SESSION_SECRET', 'ad' * 32)
    clock = tmp_path / 'clock.txt'
    clock.write_text(format_time(START), encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.setenv('TEST_CLOCK_FILE', str(clock))
    return isolated_db


@pytest.fixture
def client(notice_db, monkeypatch):
    monkeypatch.setattr(app.state, 'settings', replace(app.state.settings,
        credentials={p: f'test-service-{p}' for p in ('d', 'm1', 'm2', 'm3')}))
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


@pytest.mark.parametrize('address,reason', [
    (None, 'EMAIL_MISSING'), ('', 'EMAIL_MISSING'), ('  ', 'EMAIL_MISSING'),
    ('person@example.test', 'QUEUED'), ('a+b@sub.example.test', 'QUEUED'),
    ('no-domain', 'EMAIL_INVALID'), ('a@', 'EMAIL_INVALID'), ('@example.test', 'EMAIL_INVALID'),
    (' a@example.test', 'EMAIL_INVALID'), ('a@example.test ', 'EMAIL_INVALID'),
    ('a\nb@example.test', 'EMAIL_INVALID'), ('a\r@example.test', 'EMAIL_INVALID'),
    ('a\x00@example.test', 'EMAIL_INVALID'), ('姓名@example.test', 'EMAIL_INVALID'),
    ('A <a@example.test>', 'EMAIL_INVALID'), ('a@example.test,b@example.test', 'EMAIL_INVALID'),
    ('a' * 65 + '@example.test', 'EMAIL_INVALID'), (123, 'EMAIL_INVALID'),
])
def test_mailbox_validation(address, reason):
    result, actual = notifications._mailbox(address)
    assert actual == reason and result == (address if reason == 'QUEUED' else None)


@pytest.mark.parametrize('operation,peer,actor,user,code', [
    (send, None, 'USER', 'u-admin', 'FORBIDDEN'), (send, 'd', 'SYSTEM', None, 'FORBIDDEN'),
    (send, 'm1', 'SYSTEM', None, 'FORBIDDEN'),
    (send, 'm3', 'USER', None, 'UNAUTHENTICATED'), (send, 'm3', 'USER', ' ', 'UNAUTHENTICATED'),
    (send, 'm3', 'SYSTEM', 'u-admin', 'INVALID_ARGUMENT'), (send, 'm3', 'bad', None, 'INVALID_ARGUMENT'),
    (get, 'd', 'USER', 'u-admin', 'FORBIDDEN'), (get, None, 'SYSTEM', None, 'FORBIDDEN'),
    (get, None, 'USER', None, 'UNAUTHENTICATED'), (get, 'm3', 'SYSTEM', 'u-admin', 'INVALID_ARGUMENT'),
])
def test_untrusted_actor_never_opens_storage(monkeypatch, operation, peer, actor, user, code):
    def fail(module):
        raise AssertionError('Storage must not be opened')
    monkeypatch.setattr(notifications, 'session_factory', fail)
    denied(code, lambda: operation(peer=peer, actor=actor, user=user))


@pytest.mark.parametrize('operation,data', [
    (send, {}), (send, payload(recipientUserIds=[])), (send, payload(title='\n')),
    (send, payload(kind='M2_APPROVED')), (get, {}), (get, {'requestId': None}),
    (get, {'requestId': 'x', 'notificationId': 'y'}), (get, {'requestId': 'x', 'sourceModule': 'M3'}),
])
def test_service_validates_input_before_storage(monkeypatch, operation, data):
    def fail(module):
        raise AssertionError('Storage must not be opened')
    monkeypatch.setattr(notifications, 'session_factory', fail)
    denied('INVALID_ARGUMENT', lambda: operation(data))


@pytest.mark.parametrize('operation', [send, get])
@pytest.mark.parametrize('error', [OperationalError('private-sql', {}, RuntimeError('private-detail')),
    InterfaceError('private-sql', {}, RuntimeError('private-detail')), SQLAlchemyTimeoutError('private-detail')])
def test_storage_failure_is_explicit_and_private(monkeypatch, operation, error):
    def fail(module):
        raise error
    monkeypatch.setattr(notifications, 'session_factory', fail)
    with pytest.raises(ApiError) as caught:
        operation()
    assert caught.value.code == 'UNAVAILABLE' and 'private' not in caught.value.message


def test_persistence_complete_output_order_and_no_side_effects(notice_db):
    before = snapshot(notice_db)
    first = send()
    assert NotificationOutput.model_validate(first).model_dump() == first
    assert first == {**{k: v for k, v in payload().items() if k != 'recipientUserIds'},
        'notificationId': first['notificationId'], 'channel': 'EMAIL', 'createdAt': format_time(START),
        'deliveries': [{'recipientUserId': u, 'status': 'PENDING', 'attemptCount': 0,
                        'lastAttemptAt': None, 'reason': 'QUEUED'} for u in payload()['recipientUserIds']]}
    assert send() == first
    assert get() == first == get({'notificationId': first['notificationId']})
    after = snapshot(notice_db)
    for table in before.keys() - {'notifications', 'notification_deliveries'}:
        assert before[table] == after[table]
    assert snapshot(notice_db) == after
    with notice_db() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1
        assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 2
        row = db.get(Notification, first['notificationId'])
        assert row.retry_limit == 2
        for delivery in db.scalars(select(NotificationDelivery)):
            assert delivery.next_attempt_at == START and delivery.recipient_email


@pytest.mark.parametrize('change', [
    {'businessId': 'other'}, {'spaceId': None}, {'kind': 'M3_CANCELLED'}, {'title': 'other'},
    {'body': 'other'}, {'recipientUserIds': ['u-student2', 'u-student1']},
    {'recipientUserIds': ['u-student1']},
])
def test_changed_input_does_not_modify_original(notice_db, change):
    send()
    before = snapshot(notice_db)
    denied('REQUEST_ID_REUSED', lambda: send(payload(**change)))
    assert snapshot(notice_db) == before


def test_repeat_preserves_snapshots_and_reports_current_per_recipient_status(notice_db):
    first = send()
    with notice_db.begin() as db:
        delivery = db.get(NotificationDelivery, (first['notificationId'], 'u-student1'))
        old_address = delivery.recipient_email
        delivery.status = 'UNKNOWN'
        delivery.attempt_count = 1
        delivery.last_attempt_at = START
        delivery.next_attempt_at = None
        delivery.reason = 'SMTP_RESULT_UNKNOWN'
        db.get(User, 'u-student1').email = 'changed@example.test'
        db.get(User, 'u-student1').account_status = 'DISABLED'
        db.get(PublicConfig, 1).notification_retry_limit = 5
    before = snapshot(notice_db)
    current = send()
    assert current == get()
    assert [d['status'] for d in current['deliveries']] == ['UNKNOWN', 'PENDING']
    assert current['deliveries'][0]['attemptCount'] == 1
    assert current['createdAt'] == first['createdAt']
    assert snapshot(notice_db) == before
    with notice_db() as db:
        assert db.get(Notification, first['notificationId']).retry_limit == 2
        assert db.get(NotificationDelivery, (first['notificationId'], 'u-student1')).recipient_email == old_address
    second = send(payload(requestId='later'))
    assert second['deliveries'][0]['reason'] == 'ACCOUNT_DISABLED'
    with notice_db() as db:
        assert db.get(Notification, second['notificationId']).retry_limit == 5


@pytest.mark.parametrize('state,email,reason', [
    ('DISABLED', 'valid@example.test', 'ACCOUNT_DISABLED'),
    ('ACTIVE', None, 'EMAIL_MISSING'), ('ACTIVE', '', 'EMAIL_MISSING'),
    ('ACTIVE', 'not-an-email', 'EMAIL_INVALID'), ('ACTIVE', 'a\r\nb@example.test', 'EMAIL_INVALID'),
])
def test_one_failed_recipient_does_not_hide_other_delivery(notice_db, state, email, reason):
    with notice_db.begin() as db:
        person = db.get(User, 'u-student1')
        person.account_status, person.email = state, email
    deliveries = send()['deliveries']
    assert deliveries[0] == {'recipientUserId': 'u-student1', 'status': 'FAILED',
        'attemptCount': 0, 'lastAttemptAt': None, 'reason': reason}
    assert deliveries[1]['status'] == 'PENDING'


@pytest.mark.parametrize('changes', [{'recipientUserIds': ['u-student1', 'missing']}, {'spaceId': 'missing'}])
def test_unknown_reference_is_atomic(notice_db, changes):
    before = snapshot(notice_db)
    denied('NOT_FOUND', lambda: send(payload(**changes)))
    assert snapshot(notice_db) == before


def test_missing_config_does_not_invent_default_or_break_repeat(notice_db):
    first = send()
    with notice_db.begin() as db:
        db.execute(delete(PublicConfig))
    assert send() == first
    denied('UNAVAILABLE', lambda: send(payload(requestId='new')))


def test_sources_share_no_requests_but_user_and_system_do(notice_db):
    m3 = send()
    assert send(actor='USER', user='u-admin') == m3
    m2 = send(payload(sourceModule='M2', kind='M2_APPROVED'), peer='m2')
    assert m2['notificationId'] != m3['notificationId']
    assert get(peer='m2') == m2 and get() == m3
    denied('NOT_FOUND', lambda: get({'notificationId': m2['notificationId']}))
    denied('CONFLICT', lambda: get(peer=None, actor='USER', user='u-admin'))
    assert get({'notificationId': m2['notificationId']}, peer=None, actor='USER', user='u-admin') == m2


@pytest.mark.parametrize('permission,operation', [('d.notify.send', send), ('d.notify.read', get)])
@pytest.mark.parametrize('change', ['grant', 'subject'])
def test_system_authority_is_rechecked(notice_db, permission, operation, change):
    send()
    with notice_db.begin() as db:
        if change == 'grant':
            db.execute(delete(SystemGrant).where(SystemGrant.module_id == 'm3', SystemGrant.permission_code == permission))
        else:
            db.get(SystemSubject, 'm3').status = 'DISABLED'
    before = snapshot(notice_db)
    denied('FORBIDDEN', operation)
    assert snapshot(notice_db) == before


@pytest.mark.parametrize('change,code', [('grant', 'FORBIDDEN'), ('role', 'FORBIDDEN'), ('user', 'UNAUTHENTICATED')])
def test_user_authority_is_rechecked_even_on_repeat(notice_db, change, code):
    send(actor='USER', user='u-admin')
    with notice_db.begin() as db:
        if change == 'grant':
            db.execute(delete(RoleGrant).where(RoleGrant.role_id == 'r-admin', RoleGrant.permission_code.in_(['d.notify.send', 'd.notify.read'])))
        elif change == 'role':
            db.get(Role, 'r-admin').status = 'DISABLED'
        else:
            db.get(User, 'u-admin').account_status = 'DISABLED'
    for action in (send, get):
        denied(code, lambda: action(actor='USER', user='u-admin'))


@pytest.mark.parametrize('scope,space,allowed', [
    (('GLOBAL', None), None, True), (('DEPARTMENT', 'dep-cs'), 'sp-cs', True),
    (('DEPARTMENT', 'dep-cs'), 'sp-school', False), (('DEPARTMENT', 'dep-cs'), None, False),
    (('SPACE', 'sp-cs'), 'sp-cs', True), (('SPACE', 'sp-cs'), 'sp-school', False),
    (('SPACE', 'sp-cs'), None, False),
])
def test_scope_on_send_and_direct_management_read(notice_db, scope, space, allowed):
    first = send(payload(spaceId=space))
    with notice_db.begin() as db:
        for grant in db.scalars(select(RoleGrant).where(RoleGrant.role_id == 'r-admin',
                RoleGrant.permission_code.in_(['d.notify.send', 'd.notify.read']))):
            grant.scope_type, grant.scope_id = scope
    actions = [lambda: send(payload(spaceId=space), actor='USER', user='u-admin'),
               lambda: get(peer=None, actor='USER', user='u-admin')]
    for action in actions:
        if allowed:
            assert action() == first
        else:
            denied('FORBIDDEN', action)


def test_query_ambiguity_ignores_invisible_sources_and_uses_current_space_owner(notice_db):
    visible = send()
    send(payload(sourceModule='M2', kind='M2_APPROVED', spaceId='sp-school'), peer='m2')
    with notice_db.begin() as db:
        grant = db.scalar(select(RoleGrant).where(RoleGrant.role_id == 'r-admin', RoleGrant.permission_code == 'd.notify.read'))
        grant.scope_type, grant.scope_id = 'DEPARTMENT', 'dep-cs'
    assert get(peer=None, actor='USER', user='u-admin') == visible
    with notice_db.begin() as db:
        # 保持 DEPARTMENT 类型，迁到已存在的另一个院系以验证当前归属。
        db.get(Space, 'sp-cs').department_id = 'dep-ee'
    denied('FORBIDDEN', lambda: get(peer=None, actor='USER', user='u-admin'))


def test_query_is_read_only_and_recipient_is_not_a_management_grant(notice_db):
    send()
    before = snapshot(notice_db)
    denied('FORBIDDEN', lambda: get(peer=None, actor='USER', user='u-student1'))
    denied('NOT_FOUND', lambda: get({'requestId': 'not-yet'}))
    get()
    assert snapshot(notice_db) == before


def test_failure_before_commit_rolls_back_all_records(notice_db, monkeypatch):
    class FailingSession(Session):
        pass
    def fail(session):
        raise OperationalError('commit', {}, RuntimeError('private-detail'))
    event.listen(FailingSession, 'before_commit', fail)
    factory = sessionmaker(bind=notice_db.kw['bind'], class_=FailingSession, expire_on_commit=False,
                          join_transaction_mode='create_savepoint')
    before = snapshot(notice_db)
    monkeypatch.setattr(notifications, 'session_factory', lambda module: factory)
    denied('UNAVAILABLE', send)
    assert snapshot(notice_db) == before


def test_uncertain_response_after_commit_can_recover_without_duplicate(notice_db, monkeypatch):
    class UncertainFactory:
        @contextmanager
        def begin(self):
            with notice_db.begin() as db:
                yield db
            raise OperationalError('commit-response', {}, RuntimeError('private-detail'))
    monkeypatch.setattr(notifications, 'session_factory', lambda module: UncertainFactory())
    denied('UNAVAILABLE', send)
    monkeypatch.setattr(notifications, 'session_factory', lambda module: notice_db)
    assert send() == get()
    with notice_db() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1


def headers(token=None, peer='m3', actor='SYSTEM'):
    value = {} if peer is None else {'X-Service-Credential': f'test-service-{peer}', 'X-Actor-Mode': actor}
    if token is not None:
        value['Authorization'] = f'Bearer {token}'
    return value


def login(client, fixture_data, account='admin'):
    result = client.post('/api/v1/d/login', json={'account': account, 'password': fixture_data['password'],
                                               'requestId': f'notify-login-{account}'})
    assert result.status_code == 200
    return result.json()['data']['credential']


def http_error(result, status, code):
    assert result.status_code == status
    body = result.json()
    assert set(body) == {'ok', 'data', 'error'}
    assert body['ok'] is False and body['data'] is None and body['error']['code'] == code


def test_http_full_contract_and_health(client, fixture_data):
    health = client.get('/health').json()['data']
    assert health['unfinishedFunctions'] == []
    assert health['workerConfigured'] is True
    response = client.post(SEND, json=payload(), headers=headers())
    assert response.status_code == 200
    result = response.json()
    assert result['ok'] is True and result['error'] is None
    assert NotificationOutput.model_validate(result['data']).model_dump() == result['data']
    assert client.post(SEND, json=payload(), headers=headers()).json() == result
    for query in ({'requestId': 'notice-request'}, {'notificationId': result['data']['notificationId']}):
        assert client.post(GET, json=query, headers=headers()).json() == result
    token = login(client, fixture_data)
    assert client.post(GET, json={'requestId': 'notice-request'}, headers=headers(token, None)).json() == result
    assert client.post(GET, json={'requestId': 'notice-request'}, headers=headers(token, 'm2', 'USER')).json() == result
    http_error(client.post(SEND, json=payload(body='changed'), headers=headers()), 409, 'REQUEST_ID_REUSED')
    assert token not in response.text
    for key in ('recipient_email', 'recipientEmail', 'input_data', 'retryLimit', 'attempt_token', 'lease_expires_at'):
        assert key not in response.text


def test_http_auth_source_and_invalid_inputs(client, fixture_data):
    first = send()
    admin, student = login(client, fixture_data), login(client, fixture_data, 'student1')
    screen = login(client, fixture_data, 'screen')
    for path, data, request_headers, status, code in [
        (SEND, payload(), headers(admin, None), 403, 'FORBIDDEN'),
        (SEND, payload(), headers(peer='m2'), 403, 'FORBIDDEN'),
        # 共同初始化给 student d.notify.send，但 screen 没有该权限。
        (SEND, payload(), headers(student, 'm3', 'USER'), 200, None),
        (SEND, payload(), headers(screen, 'm3', 'USER'), 403, 'FORBIDDEN'),
        (GET, {'requestId': 'notice-request'}, headers(student, None), 403, 'FORBIDDEN'),
        (GET, {'requestId': 'notice-request'}, headers('invalid', 'm3', 'USER'), 401, 'UNAUTHENTICATED'),
        (GET, {'requestId': 'notice-request'}, headers(), 200, None),
        (GET, {'requestId': 'notice-request'}, headers(peer='m2'), 404, 'NOT_FOUND'),
        (GET, {'requestId': 'notice-request'}, {}, 401, 'UNAUTHENTICATED'),
        (GET, {'requestId': 'notice-request'}, headers(admin), 422, 'INVALID_ARGUMENT'),
        (GET, {'requestId': None}, headers(), 422, 'INVALID_ARGUMENT'),
        (GET, {'requestId': 'x', 'notificationId': 'x'}, headers(), 422, 'INVALID_ARGUMENT'),
        (SEND, payload(userId='u-admin'), headers(), 422, 'INVALID_ARGUMENT'),
        (SEND, payload(recipientUserIds=['unknown']), headers(), 409, 'REQUEST_ID_REUSED'),
        (SEND, payload(requestId='unknown', recipientUserIds=['unknown']), headers(), 404, 'NOT_FOUND'),
    ]:
        response = client.post(path, json=data, headers=request_headers)
        if code is None:
            assert response.status_code == status
            assert response.json() == {'ok': True, 'data': first, 'error': None}
        else:
            http_error(response, status, code)


def test_http_storage_failure_has_no_internal_details(client, monkeypatch):
    def fail(module):
        raise OperationalError('private-sql', {}, RuntimeError('private-detail'))
    monkeypatch.setattr(notifications, 'session_factory', fail)
    for path, data in ((SEND, payload()), (GET, {'requestId': 'x'})):
        response = client.post(path, json=data, headers=headers())
        http_error(response, 503, 'UNAVAILABLE')
        assert 'private' not in response.text
