"""D-S05严格契约与请求记录表的真实约束/无损迁移。"""
from datetime import datetime, timezone
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from pydantic import ValidationError
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.d.models import Base, RoleRequest
from backend.d.role_schemas import ROLE_INPUT
import backend.d.seed as seed_module
from backend.shared.settings import ROOT

GRANT = {'permissionCode': 'd.master.read', 'scope': {'type': 'GLOBAL', 'id': None}}
CASES = [
    {'operation': 'LIST_PERMISSIONS'}, {'operation': 'LIST_USERS'}, {'operation': 'LIST_ROLES'},
    {'operation': 'GET_ROLE', 'roleId': 'r'}, {'operation': 'GET_USER_ROLES', 'userId': 'u'},
    {'operation': 'CREATE_ROLE', 'requestId': 'create', 'name': 'custom', 'grants': [GRANT]},
    {'operation': 'UPDATE_ROLE', 'requestId': 'update', 'roleId': 'r', 'expectedVersion': 1,
     'name': 'custom', 'status': 'ACTIVE', 'grants': []},
    {'operation': 'DISABLE_ROLE', 'requestId': 'disable', 'roleId': 'r', 'expectedVersion': 1},
    {'operation': 'SET_USER_ROLES', 'requestId': 'set', 'userId': 'u', 'expectedVersion': 1, 'roleIds': ['r']},
]


@pytest.mark.parametrize('value,field', [(data, field) for data in CASES for field in data])
def test_each_required_field(value, field):
    with pytest.raises(ValidationError):
        ROLE_INPUT.validate_python({k: v for k, v in value.items() if k != field})


@pytest.mark.parametrize('value,change', [
    (CASES[0], {'limit': 0}), (CASES[0], {'limit': 101}), (CASES[0], {'limit': True}),
    (CASES[0], {'limit': '2'}), (CASES[0], {'cursor': ''}), (CASES[0], {'cursor': None}),
    (CASES[0], {'cursor': 'x' * 8193}), (CASES[0], {'requestId': 'x'}),
    (CASES[1], {'departmentId': None}), (CASES[1], {'departmentId': ' '}),
    (CASES[1], {'accountStatus': 'DELETED'}), (CASES[2], {'status': None}),
    (CASES[3], {'roleId': ' '}), (CASES[3], {'limit': 1}), (CASES[4], {'userId': None}),
    (CASES[5], {'requestId': ' '}), (CASES[5], {'name': ' '}), (CASES[5], {'name': 2}),
    (CASES[5], {'grants': None}), (CASES[5], {'grants': [GRANT, GRANT]}),
    (CASES[5], {'grants': [{'permissionCode': ' ', 'scope': GRANT['scope']}]}),
    (CASES[5], {'grants': [{'permissionCode': 'p', 'scope': {'type': 'GLOBAL', 'id': 's'}}]}),
    (CASES[5], {'grants': [{'permissionCode': 'p', 'scope': {'type': 'SPACE', 'id': None}}]}),
    (CASES[5], {'grants': [{'permissionCode': 'p', 'scope': {'type': 'DEPARTMENT', 'id': ' '}}]}),
    (CASES[5], {'grants': [GRANT | {'extra': True}]}), (CASES[5], {'expectedVersion': 1}),
    (CASES[6], {'expectedVersion': True}), (CASES[6], {'expectedVersion': 0}),
    (CASES[6], {'expectedVersion': '1'}), (CASES[6], {'status': None}),
    (CASES[7], {'name': 'no'}), (CASES[8], {'roleIds': None}), (CASES[8], {'roleIds': ['r', 'r']}),
    (CASES[8], {'roleIds': [' ']}), (CASES[8], {'roleIds': [1]}), (CASES[8], {'sourceModule': 'M1'}),
    (CASES[8], {'actorId': 'u-admin'}), (CASES[0], {'operation': 'DELETE_ROLE'}),
])
def test_strict_invalid_fields(value, change):
    with pytest.raises(ValidationError):
        ROLE_INPUT.validate_python(value | change)


def test_defaults_empty_sets_and_canonical_order():
    for data in CASES[:3]:
        result = ROLE_INPUT.validate_python(data)
        assert result.limit == 20 and result.cursor is None
    assert ROLE_INPUT.validate_python(CASES[5] | {'grants': []}).grants == []
    assert ROLE_INPUT.validate_python(CASES[8] | {'roleIds': []}).roleIds == []
    assert ROLE_INPUT.validate_python(CASES[8] | {'roleIds': ['z', 'a']}).roleIds == ['a', 'z']


@pytest.fixture
def receipt_db(isolated_db):
    seed_module.seed()
    return isolated_db


def row():
    return RoleRequest(request_id='req', input_data={}, response_data={}, requested_by_user_id='u-admin',
                       created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize('field,value,code', [
    ('request_id', ' ', '23514'), ('input_data', [], '23514'), ('response_data', [], '23514'),
    ('input_data', None, '23514'), ('requested_by_user_id', 'missing', '23503'),
    ('requested_by_user_id', None, '23502'), ('created_at', None, '23502'),
])
def test_request_table_constraints(receipt_db, field, value, code):
    record = row()
    setattr(record, field, value)
    with pytest.raises(IntegrityError) as caught:
        with receipt_db.begin() as db:
            db.add(record)
    assert caught.value.orig.sqlstate == code


def test_request_id_is_unique_across_all_role_operations(receipt_db):
    with receipt_db.begin() as db:
        db.add(row())
    with pytest.raises(IntegrityError) as caught:
        with receipt_db.begin() as db:
            db.add(row())
    assert caught.value.orig.sqlstate == '23505'


def test_migration_roundtrip_preserves_existing_roles_versions_and_data(receipt_db):
    def snapshot():
        with receipt_db() as db:
            return {t.name: db.execute(select(t).order_by(*t.primary_key.columns)).all()
                    for t in Base.metadata.sorted_tables if t.name != 'role_requests'}
    before = snapshot()
    assert len(before) == 18
    migration = ScriptDirectory(str(ROOT / 'backend/d/migrations')).get_revision('d_67f49b12a3d0').module
    with receipt_db.begin() as db:
        assert re.fullmatch(r'd_seed_test_[0-9a-f]{32}', db.scalar(text('SELECT current_schema()')))
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.downgrade()
            migration.upgrade()
        inspector = inspect(db.connection())
        assert {c['name'] for c in inspector.get_columns('role_requests')} == set(RoleRequest.__table__.columns.keys())
        assert {c['name'] for c in inspector.get_check_constraints('role_requests')} == {
            c.name for c in RoleRequest.__table__.constraints if c.__class__.__name__ == 'CheckConstraint'}
    assert snapshot() == before
