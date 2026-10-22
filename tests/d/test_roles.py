"""S05真实事务、权限与版本、集合替换、查询分页和HTTP。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from backend.d import roles as service, credentials, sessions
from backend.d import login as login_service
from backend.d.identity import build_identity
from backend.d.main import app
from backend.d.models import Base, Permission, Role, RoleGrant, RoleRequest, User, UserRole
from backend.d.role_schemas import ROLE_INPUT, RoleOutput
import backend.d.seed as seed_module
from backend.shared.errors import ApiError

PATH = '/api/v1/d/manage_roles'
GLOBAL = {'type': 'GLOBAL', 'id': None}
GRANT = {'permissionCode': 'd.master.read', 'scope': GLOBAL}
READS = [
    {'operation': 'LIST_PERMISSIONS'}, {'operation': 'LIST_USERS'}, {'operation': 'LIST_ROLES'},
    {'operation': 'GET_ROLE', 'roleId': 'r-student'}, {'operation': 'GET_USER_ROLES', 'userId': 'u-student1'},
]
WRITES = [
    {'operation': 'CREATE_ROLE', 'requestId': 'create', 'name': 'custom', 'grants': [GRANT]},
    {'operation': 'UPDATE_ROLE', 'requestId': 'update', 'roleId': 'r-student', 'expectedVersion': 1,
     'name': 'changed', 'status': 'ACTIVE', 'grants': []},
    {'operation': 'DISABLE_ROLE', 'requestId': 'disable', 'roleId': 'r-student', 'expectedVersion': 1},
    {'operation': 'SET_USER_ROLES', 'requestId': 'set', 'userId': 'u-student1', 'expectedVersion': 1, 'roleIds': []},
]


def perform(data, user='u-admin', peer='m1', mode='USER'):
    return service.manage_roles(input=data, actor_mode=mode, source_module=peer, user_id=user)


def create(**changes):
    return perform(WRITES[0] | changes)


def denied(code, action):
    with pytest.raises(ApiError) as caught:
        action()
    assert caught.value.code == code


def snapshot(factory):
    with factory() as db:
        return {t.name: db.execute(select(t).order_by(*t.primary_key.columns)).all() for t in Base.metadata.sorted_tables}


@pytest.fixture
def roles_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()
    for module in (service, login_service, sessions):
        monkeypatch.setattr(module, 'session_factory', lambda module: isolated_db)
    monkeypatch.setattr(credentials, 'ROOT', tmp_path)
    monkeypatch.setenv('D_SESSION_SECRET', 'bd' * 32)
    clock = tmp_path / 'clock.txt'
    clock.write_text('2026-01-01T00:00:00Z', encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.setenv('TEST_CLOCK_FILE', str(clock))
    return isolated_db


@pytest.mark.parametrize('peer,mode,user,code', [
    (None, 'USER', 'u-admin', 'FORBIDDEN'), ('m2', 'USER', 'u-admin', 'FORBIDDEN'),
    ('m3', 'USER', 'u-admin', 'FORBIDDEN'), ('m1', 'SYSTEM', None, 'FORBIDDEN'),
    ('m1', 'other', 'u-admin', 'FORBIDDEN'), ('m1', 'USER', None, 'UNAUTHENTICATED'),
    ('m1', 'USER', ' ', 'UNAUTHENTICATED'),
])
def test_actor_guard_before_storage(monkeypatch, peer, mode, user, code):
    def fail(module):
        pytest.fail('Untrusted actor reached storage')
    monkeypatch.setattr(service, 'session_factory', fail)
    denied(code, lambda: perform(READS[0], user=user, peer=peer, mode=mode))


@pytest.mark.parametrize('data', READS + WRITES)
@pytest.mark.parametrize('error', [InterfaceError, OperationalError, SQLAlchemyTimeoutError])
def test_storage_failure_has_safe_context_and_no_empty_success(monkeypatch, data, error, caplog):
    def fail(module):
        raise error('private-password') if error is SQLAlchemyTimeoutError else error('private-sql', {}, Exception('private-password'))
    monkeypatch.setattr(service, 'session_factory', fail)
    denied('UNAVAILABLE', lambda: perform(data))
    assert 'D S05' in caplog.text and data['operation'] in caplog.text and 'private' not in caplog.text


@pytest.mark.parametrize('data', READS + WRITES)
def test_every_operation_rejects_student_and_disabled_caller(roles_db, data):
    before = snapshot(roles_db)
    denied('FORBIDDEN', lambda: perform(data, user='u-student1'))
    with roles_db.begin() as db:
        db.get(User, 'u-admin').account_status = 'DISABLED'
    denied('UNAUTHENTICATED', lambda: perform(data))
    with roles_db.begin() as db:
        db.get(User, 'u-admin').account_status = 'ACTIVE'
    assert snapshot(roles_db) == before


@pytest.mark.parametrize('writing', [False, True])
@pytest.mark.parametrize('kind,identifier', [('SPACE', 'sp-cs'), ('DEPARTMENT', 'dep-cs')])
def test_local_grant_cannot_manage_global_roles(roles_db, writing, kind, identifier):
    code = 'd.rbac.manage' if writing else 'd.rbac.read'
    with roles_db.begin() as db:
        db.get(Permission, code).allowed_scope_types = ['GLOBAL', 'DEPARTMENT', 'SPACE']
        grant = db.scalar(select(RoleGrant).where(RoleGrant.role_id == 'r-admin', RoleGrant.permission_code == code))
        grant.scope_type, grant.scope_id = kind, identifier
    denied('FORBIDDEN', lambda: perform(WRITES[0] if writing else READS[0]))


@pytest.mark.parametrize('missing', ['d.rbac.manage', 'd.rbac.read'])
def test_read_and_write_permissions_are_independent(roles_db, missing):
    with roles_db.begin() as db:
        db.execute(delete(RoleGrant).where(RoleGrant.role_id == 'r-admin', RoleGrant.permission_code == missing))
    if missing.endswith('manage'):
        assert perform(READS[0])['items']
        denied('FORBIDDEN', lambda: create())
    else:
        assert create()['version'] == 1
        denied('FORBIDDEN', lambda: perform(READS[0]))


def test_role_lifecycle_idempotency_and_original_results(roles_db):
    first = create()
    assert first == {'roleId': first['roleId'], 'name': 'custom', 'status': 'ACTIVE', 'grants': [GRANT], 'version': 1}
    update = {'operation': 'UPDATE_ROLE', 'requestId': 'update', 'roleId': first['roleId'], 'expectedVersion': 1,
              'name': 'renamed', 'status': 'ACTIVE', 'grants': []}
    second = perform(update)
    assert second == first | {'name': 'renamed', 'grants': [], 'version': 2}
    disable = {'operation': 'DISABLE_ROLE', 'requestId': 'disable', 'roleId': first['roleId'], 'expectedVersion': 2}
    third = perform(disable)
    assert third == second | {'status': 'DISABLED', 'version': 3}
    assert create() == first and perform(update) == second and perform(disable) == third
    assert perform({'operation': 'GET_ROLE', 'roleId': first['roleId']}) == third
    denied('CONFLICT', lambda: perform(update | {'requestId': 'stale'}))
    fourth = perform(update | {'requestId': 'reactivate', 'expectedVersion': 3, 'grants': [GRANT]})
    assert fourth['status'] == 'ACTIVE' and fourth['version'] == 4 and fourth['grants'] == [GRANT]
    with roles_db() as db:
        assert db.scalar(select(func.count()).select_from(RoleRequest)) == 4
        assert db.get(RoleRequest, 'create').requested_by_user_id == 'u-admin'
        assert db.get(RoleRequest, 'create').created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize('data', WRITES)
def test_every_mutation_is_atomic_and_replays_once(roles_db, data):
    result = perform(data)
    before = snapshot(roles_db)
    assert perform(data) == result
    assert snapshot(roles_db) == before
    seed_module.seed()
    assert snapshot(roles_db) == before


@pytest.mark.parametrize('changes', [{'name': 'different'}, {'grants': []}])
def test_create_request_changed_input_rejected(roles_db, changes):
    create()
    before = snapshot(roles_db)
    denied('REQUEST_ID_REUSED', lambda: create(**changes))
    assert snapshot(roles_db) == before


def test_operation_target_version_and_collections_are_part_of_request(roles_db):
    perform(WRITES[1])
    for change in ({'roleId': 'r-admin'}, {'expectedVersion': 2}, {'status': 'DISABLED'}, {'grants': [GRANT]}):
        denied('REQUEST_ID_REUSED', lambda: perform(WRITES[1] | change))
    denied('REQUEST_ID_REUSED', lambda: perform(WRITES[2] | {'requestId': 'update'}))
    perform(WRITES[3])
    for change in ({'userId': 'u-student2'}, {'roleIds': ['r-student']}, {'expectedVersion': 2}):
        denied('REQUEST_ID_REUSED', lambda: perform(WRITES[3] | change))


def test_grants_sets_canonicalize_order_and_replace_fully(roles_db):
    other = {'permissionCode': 'd.master.read', 'scope': {'type': 'SPACE', 'id': 'sp-cs'}}
    first = create(grants=[other, GRANT])
    assert first['grants'] == [GRANT, other]
    assert create(grants=[GRANT, other]) == first
    updated = perform({'operation': 'UPDATE_ROLE', 'requestId': 'replace', 'roleId': first['roleId'],
                      'expectedVersion': 1, 'name': 'next', 'status': 'DISABLED', 'grants': [other]})
    assert updated['grants'] == [other] and updated['status'] == 'DISABLED'
    with roles_db() as db:
        assert db.scalar(select(func.count()).select_from(RoleGrant).where(RoleGrant.role_id == first['roleId'])) == 1


@pytest.mark.parametrize('grant,code', [
    ({'permissionCode': 'missing', 'scope': GLOBAL}, 'NOT_FOUND'),
    ({'permissionCode': 'd.master.read', 'scope': {'type': 'SPACE', 'id': 'missing'}}, 'NOT_FOUND'),
    ({'permissionCode': 'd.master.read', 'scope': {'type': 'DEPARTMENT', 'id': 'missing'}}, 'NOT_FOUND'),
    ({'permissionCode': 'd.config.manage', 'scope': {'type': 'SPACE', 'id': 'sp-cs'}}, 'INVALID_ARGUMENT'),
])
@pytest.mark.parametrize('updating', [False, True])
def test_invalid_grant_rolls_back_entire_replacement(roles_db, grant, code, updating):
    before = snapshot(roles_db)
    data = (WRITES[1] if updating else WRITES[0]) | {'grants': [GRANT, grant]}
    denied(code, lambda: perform(data))
    assert snapshot(roles_db) == before


def test_user_sets_versions_empty_and_disabled_history(roles_db):
    original = perform({'operation': 'GET_USER_ROLES', 'userId': 'u-student1'})
    assert original == {'userId': 'u-student1', 'roleIds': ['r-student'], 'version': 1}
    extra = create()
    request = WRITES[3] | {'roleIds': [extra['roleId'], 'r-student']}
    assigned = perform(request)
    assert assigned['roleIds'] == sorted(request['roleIds']) and assigned['version'] == 2
    assert perform(request | {'roleIds': list(reversed(request['roleIds']))}) == assigned
    perform({'operation': 'DISABLE_ROLE', 'requestId': 'disable', 'roleId': extra['roleId'], 'expectedVersion': 1})
    assert perform({'operation': 'GET_USER_ROLES', 'userId': 'u-student1'}) == assigned
    with roles_db() as db:
        assert extra['roleId'] not in build_identity(db, 'u-student1')['roleIds']
    retained = perform(request | {'requestId': 'retain', 'expectedVersion': 2})
    assert retained == assigned | {'version': 3}
    denied('CONFLICT', lambda: perform(request | {'requestId': 'other', 'userId': 'u-student2'}))
    empty = perform(WRITES[3] | {'requestId': 'clear', 'expectedVersion': 3})
    assert empty == {'userId': 'u-student1', 'roleIds': [], 'version': 4}
    assert perform({'operation': 'GET_USER_ROLES', 'userId': 'u-student1'}) == empty
    denied('CONFLICT', lambda: perform(request | {'requestId': 'stale'}))
    # 新请求即使集合不变也增加版本，旧请求不会覆盖后续清空。
    assert perform(request) == assigned
    assert perform(WRITES[3] | {'requestId': 'clear-again', 'expectedVersion': 4})['version'] == 5


def test_new_empty_user_roles_version_one_and_disabled_target_supported(roles_db):
    with roles_db.begin() as db:
        user = db.get(User, 'u-student2')
        user.account_status = 'DISABLED'
        db.execute(delete(UserRole).where(UserRole.user_id == user.user_id))
    assert perform({'operation': 'GET_USER_ROLES', 'userId': 'u-student2'}) == {
        'userId': 'u-student2', 'roleIds': [], 'version': 1}
    result = perform(WRITES[3] | {'userId': 'u-student2', 'roleIds': ['r-student']})
    assert result['version'] == 2
    with roles_db() as db:
        assert db.get(User, 'u-student2').account_status == 'DISABLED'


@pytest.mark.parametrize('data', [
    {'operation': 'GET_ROLE', 'roleId': 'missing'}, {'operation': 'GET_USER_ROLES', 'userId': 'missing'},
    {'operation': 'LIST_USERS', 'departmentId': 'missing'}, WRITES[1] | {'roleId': 'missing'},
    WRITES[2] | {'roleId': 'missing'}, WRITES[3] | {'userId': 'missing'},
    WRITES[3] | {'roleIds': ['r-student', 'missing']},
])
def test_unknown_references_are_404_without_partial_changes(roles_db, data):
    before = snapshot(roles_db)
    denied('NOT_FOUND', lambda: perform(data))
    assert snapshot(roles_db) == before


def test_current_identity_changes_and_permission_revocation_before_replay(roles_db):
    first = create(grants=[])
    perform(WRITES[3] | {'roleIds': [first['roleId']]})
    with roles_db() as db:
        assert build_identity(db, 'u-student1')['grants'] == []
    perform({'operation': 'UPDATE_ROLE', 'roleId': first['roleId'], 'expectedVersion': 1,
             'requestId': 'grant', 'name': first['name'], 'status': 'ACTIVE', 'grants': [GRANT]})
    with roles_db() as db:
        assert build_identity(db, 'u-student1')['grants'] == [GRANT]
    remove = WRITES[3] | {'requestId': 'remove-my-roles', 'userId': 'u-admin'}
    assert perform(remove)['roleIds'] == []
    for data in [remove, WRITES[0], READS[0]]:
        denied('FORBIDDEN', lambda: perform(data))


def test_same_request_shared_by_current_authorized_m1_users(roles_db):
    first = create()
    perform(WRITES[3] | {'roleIds': ['r-admin']})
    assert perform(WRITES[0], user='u-student1') == first
    with roles_db() as db:
        assert db.get(RoleRequest, 'create').requested_by_user_id == 'u-admin'


@pytest.mark.parametrize('operation,key', [('LIST_PERMISSIONS', 'code'), ('LIST_USERS', 'userId'), ('LIST_ROLES', 'roleId')])
def test_pages_stable_bound_and_read_only(roles_db, operation, key):
    before = snapshot(roles_db)
    query = {'operation': operation, 'limit': 2}
    page = perform(query)
    first_cursor = page['nextCursor']
    assert len(page['items']) == 2 and first_cursor
    items = list(page['items'])
    while page['nextCursor']:
        page = perform(query | {'cursor': page['nextCursor']})
        items.extend(page['items'])
    assert items == perform({'operation': operation, 'limit': 100})['items']
    assert [r[key] for r in items] == sorted({r[key] for r in items})
    for change in ({'limit': 1}, {'cursor': first_cursor + 'x'}):
        denied('INVALID_ARGUMENT', lambda: perform(query | {'cursor': first_cursor} | change))
    assert snapshot(roles_db) == before
    perform(WRITES[3] | {'roleIds': ['r-admin']})
    denied('INVALID_ARGUMENT', lambda: perform(query | {'cursor': first_cursor}, user='u-student1'))


def test_filters_catalogue_exact_fields_and_cursor_operation(roles_db, fixture_data):
    with roles_db.begin() as db:
        db.get(User, 'u-student1').account_status = 'DISABLED'
        db.get(Role, 'r-student').status = 'DISABLED'
    assert perform({'operation': 'LIST_USERS', 'departmentId': 'dep-cs', 'accountStatus': 'DISABLED'})['items'] == [{
        'userId': 'u-student1', 'displayName': next(u['displayName'] for u in fixture_data['users'] if u['userId'] == 'u-student1'),
        'departmentId': 'dep-cs', 'accountStatus': 'DISABLED'}]
    assert perform({'operation': 'LIST_ROLES', 'status': 'DISABLED'})['items'] == [perform(READS[3])]
    permissions = perform({'operation': 'LIST_PERMISSIONS', 'limit': 100})['items']
    assert [p['code'] for p in permissions] == sorted(fixture_data['permissions'])
    assert all(set(p) == {'code', 'name', 'allowedScopeTypes'} and p['allowedScopeTypes'] == sorted(
        fixture_data['permissionScopeTypes'][p['code']]) for p in permissions)
    cursor = perform({'operation': 'LIST_ROLES', 'limit': 1})['nextCursor']
    denied('INVALID_ARGUMENT', lambda: perform({'operation': 'LIST_USERS', 'limit': 1, 'cursor': cursor}))
    denied('INVALID_ARGUMENT', lambda: perform({'operation': 'LIST_ROLES', 'limit': 1, 'status': 'ACTIVE', 'cursor': cursor}))
    users = perform({'operation': 'LIST_USERS', 'limit': 100})['items']
    assert all(set(u) == {'userId', 'displayName', 'departmentId', 'accountStatus'} for u in users)


def test_commit_failure_rolls_back_and_uncertain_result_can_replay(roles_db, monkeypatch):
    class FailingSession(Session):
        pass
    def fail(db):
        raise OperationalError('commit', {}, Exception('private'))
    event.listen(FailingSession, 'before_commit', fail)
    factory = sessionmaker(bind=roles_db.kw['bind'], class_=FailingSession, expire_on_commit=False,
                          join_transaction_mode='create_savepoint')
    before = snapshot(roles_db)
    monkeypatch.setattr(service, 'session_factory', lambda module: factory)
    denied('UNAVAILABLE', create)
    assert snapshot(roles_db) == before
    class Uncertain:
        @contextmanager
        def begin(self):
            with roles_db.begin() as db:
                yield db
            raise OperationalError('commit-response', {}, Exception('private'))
    monkeypatch.setattr(service, 'session_factory', lambda module: Uncertain())
    denied('UNAVAILABLE', create)
    monkeypatch.setattr(service, 'session_factory', lambda module: roles_db)
    result = create()
    assert perform({'operation': 'GET_ROLE', 'roleId': result['roleId']}) == result
    with roles_db() as db:
        assert db.scalar(select(func.count()).select_from(RoleRequest)) == 1


def test_http_complete_contract_auth_health_and_all_operations(roles_db, monkeypatch, fixture_data):
    monkeypatch.setattr(app.state, 'settings', replace(app.state.settings,
        credentials={p: f'test-service-{p}' for p in ('d', 'm1', 'm2', 'm3')}))
    with TestClient(app, raise_server_exceptions=False) as client:
        tokens = {}
        for account in ('admin', 'student1'):
            response = client.post('/api/v1/d/login', json={'account': account, 'password': fixture_data['password'],
                                                          'requestId': f'roles-login-{account}'})
            assert response.status_code == 200
            tokens[account] = response.json()['data']['credential']
        headers = {'X-Service-Credential': 'test-service-m1', 'X-Actor-Mode': 'USER',
                   'Authorization': f'Bearer {tokens["admin"]}'}
        for data in READS + WRITES:
            if data['operation'] == 'DISABLE_ROLE':
                data = data | {'expectedVersion': 2}
            response = client.post(PATH, json=data, headers=headers)
            assert response.status_code == 200
            result = response.json()
            assert set(result) == {'ok', 'data', 'error'} and result['ok'] is True and result['error'] is None
            assert not any(secret in result['data'] for secret in ('password', 'email', 'account', 'input_data'))
        health = client.get('/health').json()['data']
        assert health['unfinishedFunctions'] == [] and health['workerConfigured'] is True
        schema = client.get('/openapi.json').json()['paths'][PATH]['post']['requestBody']['content']['application/json']['schema']
        assert schema['discriminator']['propertyName'] == 'operation' and len(schema['oneOf']) == 9
        for data, hdr, status, code in [
            (READS[0], {}, 403, 'FORBIDDEN'),
            (READS[0], {'Authorization': headers['Authorization']}, 403, 'FORBIDDEN'),
            (READS[0], headers | {'X-Service-Credential': 'test-service-m2'}, 403, 'FORBIDDEN'),
            (READS[0], {'X-Service-Credential': 'test-service-m1', 'X-Actor-Mode': 'SYSTEM'}, 403, 'FORBIDDEN'),
            (READS[0], headers | {'Authorization': 'Bearer invalid'}, 401, 'UNAUTHENTICATED'),
            (READS[0], headers | {'Authorization': f'Bearer {tokens["student1"]}'}, 403, 'FORBIDDEN'),
            ({'operation': 'LIST_ROLES', 'status': None}, headers, 422, 'INVALID_ARGUMENT'),
            (WRITES[0] | {'name': 'changed'}, headers, 409, 'REQUEST_ID_REUSED'),
            (WRITES[1] | {'requestId': 'old-version'}, headers, 409, 'CONFLICT'),
            ({'operation': 'GET_ROLE', 'roleId': 'missing'}, headers, 404, 'NOT_FOUND'),
        ]:
            response = client.post(PATH, json=data, headers=hdr)
            assert response.status_code == status and response.json()['error']['code'] == code


@pytest.mark.parametrize('operation,model,column_name,output_key', [
    ('LIST_PERMISSIONS', Permission, 'code', 'code'),
    ('LIST_USERS', User, 'user_id', 'userId'),
    ('LIST_ROLES', Role, 'role_id', 'roleId'),
])
def test_collation_mixed_identifiers_and_cursor_boundaries(roles_db, operation, model, column_name, output_key):
    # 标点、大小写、重音和中文必须使用与请求规范化相同的字符顺序。
    identifiers = ['r-order', 'role-order', 'order-a', 'order_a', 'ordera', 'Order-b', 'order-É', 'order-中']
    column = getattr(model, column_name)
    with roles_db.begin() as db:
        baseline = db.scalars(select(column)).all()
        template = db.get(User, 'u-admin')
        for index, identifier in enumerate(identifiers):
            if model is Permission:
                db.add(Permission(code=identifier, name=identifier, allowed_scope_types=['GLOBAL']))
            elif model is Role:
                db.add(Role(role_id=identifier, name=identifier, status='ACTIVE', version=1))
            else:
                db.add(User(user_id=identifier, account=f'collation-account-{index}',
                    password_hash=template.password_hash, display_name=identifier, department_id=None,
                    email=None, account_status='ACTIVE', role_assignment_version=1))
    expected = sorted([*baseline, *identifiers])
    page = perform({'operation': operation, 'limit': 3})
    found = list(page['items'])
    seen = set()
    while page['nextCursor'] is not None:
        assert page['nextCursor'] not in seen and len(seen) < 100
        seen.add(page['nextCursor'])
        page = perform({'operation': operation, 'limit': 3, 'cursor': page['nextCursor']})
        found.extend(page['items'])
    assert [item[output_key] for item in found] == expected
    assert [item[output_key] for item in perform({'operation': operation, 'limit': 100})['items']] == expected


def test_collation_grants_and_role_sets_match_canonical_requests(roles_db):
    identifiers = ['r-order', 'role-order', 'order_a', 'Order-b', 'order-É', 'order-中']
    with roles_db.begin() as db:
        for identifier in identifiers:
            db.add(Role(role_id=identifier, name=identifier, status='ACTIVE', version=1))
            db.add(Permission(code=identifier, name=identifier, allowed_scope_types=['GLOBAL', 'SPACE']))
    grants = [{'permissionCode': identifier, 'scope': scope} for identifier in identifiers
              for scope in (GLOBAL, {'type': 'SPACE', 'id': 'sp-cs'})]
    expected_grants = sorted(grants, key=lambda item: (
        item['permissionCode'], item['scope']['type'], item['scope']['id'] or ''))
    role = create(grants=list(reversed(grants)))
    assert role['grants'] == expected_grants
    assert create(grants=grants) == role
    assert perform({'operation': 'GET_ROLE', 'roleId': role['roleId']}) == role
    query = WRITES[3] | {'roleIds': list(reversed(identifiers))}
    assigned = perform(query)
    assert assigned == {'userId': 'u-student1', 'roleIds': sorted(identifiers), 'version': 2}
    assert perform(query | {'roleIds': identifiers}) == assigned
    assert perform({'operation': 'GET_USER_ROLES', 'userId': 'u-student1'}) == assigned


def test_collation_old_sorting_cursor_requires_restart(roles_db):
    from backend.d.pagination import encode_cursor
    query = ROLE_INPUT.validate_python({'operation': 'LIST_ROLES', 'limit': 1})
    old_context = {'service': 'd.manage_roles', 'query': query.model_dump(exclude={'cursor'}),
                   'subjectId': 'u-admin', 'sourceModule': 'm1', 'scopes': [GLOBAL]}
    cursor = encode_cursor('r-admin', old_context)
    denied('INVALID_ARGUMENT', lambda: perform({'operation': 'LIST_ROLES', 'limit': 1, 'cursor': cursor}))
