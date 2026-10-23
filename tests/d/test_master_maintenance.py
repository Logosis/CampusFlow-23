"""S07真实事务：范围权限、生命周期、历史占号、版本、幂等及HTTP。"""
from contextlib import contextmanager
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from backend.d import master_maintenance as service, master, credentials, sessions
from backend.d import login as login_service
from backend.d.main import app
from backend.d.models import Base, MasterRequest, Role, RoleGrant, Seat, SeatCodeHistory, Space, User, UserRole
import backend.d.seed as seed_module
from backend.shared.errors import ApiError

PATH = '/api/v1/d/maintain_master'
SPACE = {'name': 'New', 'location': 'Test room', 'ownerKind': 'SCHOOL', 'departmentId': None,
         'isStudyRoom': True, 'enabled': True}
SEAT = {'seatCode': 'TEST-NEW', 'enabled': True, 'socketTags': ['RAIL', 'FIXED']}
CASES = [
    {'operation': 'CREATE_SPACE', 'requestId': 'new-space', **SPACE},
    {'operation': 'UPDATE_SPACE', 'requestId': 'edit-space', 'spaceId': 'sp-school', 'expectedVersion': 1, **SPACE},
    {'operation': 'UNREGISTER_SPACE', 'requestId': 'remove-space', 'spaceId': 'sp-school', 'expectedVersion': 1},
    {'operation': 'CREATE_SEAT', 'requestId': 'new-seat', 'spaceId': 'sp-school', **SEAT},
    {'operation': 'UPDATE_SEAT', 'requestId': 'edit-seat', 'seatId': 'seat-school-1', 'expectedVersion': 1, **SEAT},
    {'operation': 'UNREGISTER_SEAT', 'requestId': 'remove-seat', 'seatId': 'seat-school-1', 'expectedVersion': 1},
]


def perform(value, user='u-admin', peer='m1', mode='USER'):
    return service.maintain_master(input=value, actor_mode=mode, source_module=peer, user_id=user)


def denied(code, action):
    with pytest.raises(ApiError) as caught:
        action()
    assert caught.value.code == code


def snapshot(factory):
    with factory() as db:
        return {t.name: db.execute(select(t).order_by(*t.primary_key.columns)).all() for t in Base.metadata.sorted_tables}


@pytest.fixture
def maintenance_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()
    for module in (service, master, login_service, sessions):
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
def test_actor_guard_precedes_storage(monkeypatch, peer, mode, user, code):
    def fail(module):
        pytest.fail('Invalid caller reached storage')
    monkeypatch.setattr(service, 'session_factory', fail)
    denied(code, lambda: perform(CASES[0], user=user, peer=peer, mode=mode))


@pytest.mark.parametrize('value', CASES)
@pytest.mark.parametrize('error', [InterfaceError, OperationalError, SQLAlchemyTimeoutError])
def test_storage_failure_reports_safe_operation_context(monkeypatch, value, error, caplog):
    def fail(module):
        raise error('private-password') if error is SQLAlchemyTimeoutError else error('private-sql', {}, Exception('private-password'))
    monkeypatch.setattr(service, 'session_factory', fail)
    denied('UNAVAILABLE', lambda: perform(value))
    assert 'D S07' in caplog.text and value['operation'] in caplog.text and 'private' not in caplog.text


@pytest.mark.parametrize('value', CASES)
def test_no_manage_permission_and_disabled_actor_do_not_write(maintenance_db, value):
    before = snapshot(maintenance_db)
    denied('FORBIDDEN', lambda: perform(value, user='u-student1'))
    with maintenance_db.begin() as db:
        db.get(User, 'u-admin').account_status = 'DISABLED'
    denied('UNAUTHENTICATED', lambda: perform(value))
    with maintenance_db.begin() as db:
        db.get(User, 'u-admin').account_status = 'ACTIVE'
    assert snapshot(maintenance_db) == before


def grant(factory, kind, identifier, user='u-student1'):
    with factory.begin() as db:
        db.add(Role(role_id='r-master-test', name='test', status='ACTIVE', version=1))
        db.flush()
        db.add(RoleGrant(role_id='r-master-test', permission_code='d.master.manage', scope_type=kind, scope_id=identifier))
        db.add(UserRole(user_id=user, role_id='r-master-test'))


@pytest.mark.parametrize('scope,identifier', [('DEPARTMENT', 'dep-cs'), ('SPACE', 'sp-cs')])
def test_scoped_manager_cannot_escape_existing_space_scope(maintenance_db, scope, identifier):
    grant(maintenance_db, scope, identifier)
    before = snapshot(maintenance_db)
    for value in CASES:
        denied('FORBIDDEN', lambda value=value: perform(value, user='u-student1'))
    assert snapshot(maintenance_db) == before
    result = perform(CASES[3] | {'spaceId': 'sp-cs'}, user='u-student1')
    assert result['spaceId'] == 'sp-cs'


def test_department_create_and_destination_ownership_authority(maintenance_db):
    grant(maintenance_db, 'DEPARTMENT', 'dep-cs')
    created = perform(CASES[0] | {'ownerKind': 'DEPARTMENT', 'departmentId': 'dep-cs'}, user='u-student1')
    update = CASES[1] | {'spaceId': created['spaceId']}
    denied('FORBIDDEN', lambda: perform(update, user='u-student1'))
    changed = perform(update)
    assert changed['ownerKind'] == 'SCHOOL' and changed['version'] == 2
    denied('FORBIDDEN', lambda: perform(CASES[0] | {'ownerKind': 'DEPARTMENT', 'departmentId': 'dep-cs'}, user='u-student1'))
    denied('FORBIDDEN', lambda: perform(update, user='u-student1'))


def test_space_scope_allows_metadata_but_not_create_or_transfer(maintenance_db):
    grant(maintenance_db, 'SPACE', 'sp-cs')
    local = CASES[1] | {'spaceId': 'sp-cs', 'ownerKind': 'DEPARTMENT', 'departmentId': 'dep-cs'}
    result = perform(local, user='u-student1')
    assert result['version'] == 2
    denied('FORBIDDEN', lambda: perform(CASES[0] | {'ownerKind': 'DEPARTMENT', 'departmentId': 'dep-cs'}, user='u-student1'))
    denied('FORBIDDEN', lambda: perform(local | {'requestId': 'move', 'expectedVersion': 2,
                                                'ownerKind': 'SCHOOL', 'departmentId': None}, user='u-student1'))


def test_space_lifecycle_replay_preserves_first_result_and_query_history(maintenance_db):
    first = perform(CASES[0])
    assert first == {'spaceId': first['spaceId'], **SPACE, 'registered': True, 'version': 1}
    assert perform(CASES[0]) == first
    updated = CASES[1] | {'spaceId': first['spaceId'], 'name': 'Changed', 'enabled': False}
    second = perform(updated)
    assert second == first | {'name': 'Changed', 'enabled': False, 'version': 2}
    assert perform(updated) == second and perform(CASES[0]) == first
    denied('REQUEST_ID_REUSED', lambda: perform(updated | {'expectedVersion': 2}))
    denied('CONFLICT', lambda: perform(updated | {'requestId': 'stale'}))
    closed_input = CASES[2] | {'spaceId': first['spaceId'], 'expectedVersion': 2}
    closed = perform(closed_input)
    assert closed == second | {'registered': False, 'version': 3}
    assert perform(closed_input) == closed and perform(updated) == second
    denied('CONFLICT', lambda: perform(updated | {'requestId': 'revive', 'expectedVersion': 3}))
    denied('CONFLICT', lambda: perform(closed_input | {'requestId': 'again', 'expectedVersion': 3}))
    query = {'operation': 'GET_SPACE', 'spaceId': first['spaceId']}
    denied('NOT_FOUND', lambda: master.query_master(input=query, actor_mode='USER', source_module='m1', user_id='u-admin'))
    assert master.query_master(input=query | {'includeUnregistered': True}, actor_mode='USER', source_module='m1', user_id='u-admin') == closed


def test_seat_lifecycle_permanent_codes_and_request_set_normalization(maintenance_db):
    first = perform(CASES[3])
    assert first == {'seatId': first['seatId'], 'spaceId': 'sp-school', **SEAT,
                     'socketTags': ['FIXED', 'RAIL'], 'registered': True, 'version': 1}
    assert perform(CASES[3] | {'socketTags': ['FIXED', 'RAIL']}) == first
    edited = CASES[4] | {'seatId': first['seatId'], 'seatCode': 'TEST-CHANGED', 'enabled': False, 'socketTags': []}
    second = perform(edited)
    assert second == first | {'seatCode': 'TEST-CHANGED', 'enabled': False, 'socketTags': [], 'version': 2}
    for code in ('TEST-NEW', 'TEST-CHANGED'):
        denied('CONFLICT', lambda code=code: perform(CASES[3] | {'requestId': 'reuse-' + code, 'seatCode': code}))
    denied('CONFLICT', lambda: perform(edited | {'requestId': 'reuse-own', 'expectedVersion': 2, 'seatCode': 'TEST-NEW'}))
    same = perform(edited | {'requestId': 'same-current', 'expectedVersion': 2})
    assert same['version'] == 3
    closed_input = CASES[5] | {'seatId': first['seatId'], 'expectedVersion': 3}
    closed = perform(closed_input)
    assert closed == same | {'registered': False, 'version': 4}
    assert perform(CASES[3]) == first and perform(edited) == second and perform(closed_input) == closed
    denied('CONFLICT', lambda: perform(edited | {'requestId': 'revive', 'expectedVersion': 4}))
    with maintenance_db() as db:
        assert set(db.scalars(select(SeatCodeHistory.seat_code))) == {'TEST-NEW', 'TEST-CHANGED'}


def test_seeded_current_code_reserved_and_remembered_before_rename(maintenance_db):
    denied('CONFLICT', lambda: perform(CASES[3] | {'seatCode': 'TEST-S-001'}))
    changed = perform(CASES[4])
    assert changed['version'] == 2
    denied('CONFLICT', lambda: perform(CASES[3] | {'seatCode': 'TEST-S-001'}))
    with maintenance_db() as db:
        assert db.get(SeatCodeHistory, 'TEST-S-001') is not None
        assert db.get(MasterRequest, 'new-seat') is None


def test_repeat_seed_preserves_renamed_codes_receipts_and_versions(maintenance_db):
    perform(CASES[4])
    before = snapshot(maintenance_db)
    seed_module.seed()
    assert snapshot(maintenance_db) == before
    denied('CONFLICT', lambda: perform(CASES[3] | {'seatCode': 'TEST-S-001'}))


def test_parent_unregister_requires_all_seats_unregistered_even_if_disabled(maintenance_db):
    before = snapshot(maintenance_db)
    denied('CONFLICT', lambda: perform(CASES[2]))
    assert snapshot(maintenance_db) == before
    with maintenance_db.begin() as db:
        db.get(Seat, 'seat-school-1').enabled = False
        db.get(Seat, 'seat-school-2').enabled = False
    denied('CONFLICT', lambda: perform(CASES[2]))
    for seat_id in ('seat-school-1', 'seat-school-2'):
        perform(CASES[5] | {'seatId': seat_id, 'requestId': 'close-' + seat_id})
    closed = perform(CASES[2])
    assert closed['registered'] is False and closed['enabled'] is False and closed['version'] == 2
    denied('CONFLICT', lambda: perform(CASES[3]))
    with maintenance_db() as db:
        assert db.get(Seat, 'seat-school-1') is not None and db.get(Seat, 'seat-school-2') is not None


def test_disabled_parent_accepts_metadata_without_enabling_parent(maintenance_db):
    perform(CASES[1] | {'enabled': False})
    seat = perform(CASES[3])
    assert seat['enabled'] is True
    with maintenance_db() as db:
        assert db.get(Space, seat['spaceId']).enabled is False


@pytest.mark.parametrize('value', [
    CASES[0] | {'ownerKind': 'DEPARTMENT', 'departmentId': 'missing'},
    CASES[1] | {'spaceId': 'missing'}, CASES[1] | {'ownerKind': 'DEPARTMENT', 'departmentId': 'missing'},
    CASES[2] | {'spaceId': 'missing'}, CASES[3] | {'spaceId': 'missing'},
    CASES[4] | {'seatId': 'missing'}, CASES[5] | {'seatId': 'missing'},
])
def test_unknown_references_do_not_leave_receipt_or_partial_changes(maintenance_db, value):
    before = snapshot(maintenance_db)
    denied('NOT_FOUND', lambda: perform(value))
    assert snapshot(maintenance_db) == before


def test_current_permissions_checked_before_returning_original_receipt(maintenance_db):
    perform(CASES[0])
    with maintenance_db.begin() as db:
        db.execute(delete(RoleGrant).where(RoleGrant.role_id == 'r-admin', RoleGrant.permission_code == 'd.master.manage'))
    before = snapshot(maintenance_db)
    denied('FORBIDDEN', lambda: perform(CASES[0]))
    assert snapshot(maintenance_db) == before


def test_mutation_namespace_shared_across_operations_and_authorized_users(maintenance_db):
    first = perform(CASES[0])
    grant(maintenance_db, 'GLOBAL', None)
    assert perform(CASES[0], user='u-student1') == first
    denied('REQUEST_ID_REUSED', lambda: perform(CASES[3] | {'requestId': CASES[0]['requestId']}))
    denied('REQUEST_ID_REUSED', lambda: perform(CASES[0] | {'name': 'changed'}))


def test_failure_after_mutation_rolls_back_data_codes_and_receipt(maintenance_db, monkeypatch):
    before = snapshot(maintenance_db)
    def fail():
        raise RuntimeError('injected receipt clock failure')
    monkeypatch.setattr(service, 'utc_now', fail)
    with pytest.raises(RuntimeError, match='injected'):
        perform(CASES[4])
    assert snapshot(maintenance_db) == before


def test_commit_unknown_retries_original_result_without_new_version(maintenance_db, monkeypatch):
    class LostResponse:
        @contextmanager
        def begin(self):
            with maintenance_db.begin() as db:
                yield db
            raise OperationalError('private-sql', {}, Exception('private-details'))
    monkeypatch.setattr(service, 'session_factory', lambda module: LostResponse())
    denied('UNAVAILABLE', lambda: perform(CASES[4]))
    with maintenance_db() as db:
        original = db.get(MasterRequest, CASES[4]['requestId']).response_data
        assert db.get(Seat, 'seat-school-1').version == 2
    monkeypatch.setattr(service, 'session_factory', lambda module: maintenance_db)
    assert perform(CASES[4]) == original
    with maintenance_db() as db:
        assert db.get(Seat, 'seat-school-1').version == 2


def test_commit_failure_rolls_back_real_transaction(maintenance_db, monkeypatch):
    before = snapshot(maintenance_db)
    with maintenance_db() as db:
        connection = db.get_bind()
    class FailingSession(Session):
        pass
    factory = sessionmaker(bind=connection, class_=FailingSession, join_transaction_mode='create_savepoint')
    @event.listens_for(FailingSession, 'before_commit')
    def fail(db):
        raise OperationalError('private-sql', {}, Exception('private-details'))
    monkeypatch.setattr(service, 'session_factory', lambda module: factory)
    denied('UNAVAILABLE', lambda: perform(CASES[3]))
    assert snapshot(maintenance_db) == before


def test_http_full_contract_current_identity_and_guards(maintenance_db, fixture_data, monkeypatch):
    monkeypatch.setattr(app.state, 'settings', replace(app.state.settings,
        credentials={p: p + '-maintenance-test' for p in ('d', 'm1', 'm2', 'm3')}))
    with TestClient(app) as client:
        def login(account):
            result = client.post('/api/v1/d/login', json={'account': account, 'password': fixture_data['password'], 'requestId': 'http-' + account})
            assert result.status_code == 200
            return result.json()['data']['credential']
        admin, student = login('admin'), login('student1')
        def headers(token=admin, peer='m1'):
            h = {'Authorization': 'Bearer ' + token}
            if peer:
                h.update({'X-Service-Credential': peer + '-maintenance-test', 'X-Actor-Mode': 'USER'})
            return h
        result = client.post(PATH, json=CASES[0], headers=headers())
        assert result.status_code == 200 and result.json()['ok'] is True and result.json()['error'] is None
        data = result.json()['data']
        assert data == {'spaceId': data['spaceId'], **SPACE, 'registered': True, 'version': 1}
        assert client.post(PATH, json=CASES[0], headers=headers()).json() == result.json()
        query = client.post('/api/v1/d/query_master', json={'operation': 'GET_SPACE', 'spaceId': data['spaceId']}, headers=headers())
        assert query.status_code == 200 and query.json()['data'] == data
        health = client.get('/health').json()['data']
        assert health['unfinishedFunctions'] == [] and health['workerConfigured'] is True
        schema = client.get('/openapi.json').json()['paths'][PATH]['post']['requestBody']['content']['application/json']['schema']
        assert schema['discriminator']['propertyName'] == 'operation' and len(schema['oneOf']) == 6
        for value, hdr, status, code in [
            (CASES[0], {}, 403, 'FORBIDDEN'), (CASES[0], headers(peer=None), 403, 'FORBIDDEN'),
            (CASES[0], headers(peer='m2'), 403, 'FORBIDDEN'), (CASES[0], headers(token=student), 403, 'FORBIDDEN'),
            (CASES[0], headers(token='unknown'), 401, 'UNAUTHENTICATED'),
            (CASES[0], {'X-Service-Credential': 'm1-maintenance-test', 'X-Actor-Mode': 'SYSTEM'}, 403, 'FORBIDDEN'),
            (CASES[0] | {'enabled': 'true'}, headers(), 422, 'INVALID_ARGUMENT'),
            (CASES[4] | {'spaceId': 'sp-cs'}, headers(), 422, 'INVALID_ARGUMENT'),
            (CASES[0] | {'name': 'changed'}, headers(), 409, 'REQUEST_ID_REUSED'),
        ]:
            response = client.post(PATH, json=value, headers=hdr)
            assert response.status_code == status and response.json()['error']['code'] == code
