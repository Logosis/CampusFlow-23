"""S07严格公开契约、永久座位号表与真实迁移保留。"""
import re
from datetime import datetime, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from pydantic import ValidationError
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.d.master_maintenance_schemas import MAINTENANCE_INPUT
from backend.d.models import Base, MasterRequest, Seat, SeatCodeHistory
import backend.d.seed as seed_module
from backend.shared.settings import ROOT

SPACE = {'name': 'Test', 'location': 'Room', 'isStudyRoom': True,
         'ownerKind': 'SCHOOL', 'departmentId': None, 'enabled': True}
SEAT = {'seatCode': 'NEW-1', 'enabled': True, 'socketTags': ['RAIL', 'FIXED']}
CASES = [
    {'operation': 'CREATE_SPACE', 'requestId': 'create-space', **SPACE},
    {'operation': 'UPDATE_SPACE', 'requestId': 'update-space', 'spaceId': 'sp-school', 'expectedVersion': 1, **SPACE},
    {'operation': 'UNREGISTER_SPACE', 'requestId': 'unregister-space', 'spaceId': 'sp-school', 'expectedVersion': 1},
    {'operation': 'CREATE_SEAT', 'requestId': 'create-seat', 'spaceId': 'sp-school', **SEAT},
    {'operation': 'UPDATE_SEAT', 'requestId': 'update-seat', 'seatId': 'seat-school-1', 'expectedVersion': 1, **SEAT},
    {'operation': 'UNREGISTER_SEAT', 'requestId': 'unregister-seat', 'seatId': 'seat-school-1', 'expectedVersion': 1},
]


@pytest.mark.parametrize('value,field', [(data, field) for data in CASES for field in data])
def test_each_required_field(value, field):
    with pytest.raises(ValidationError):
        MAINTENANCE_INPUT.validate_python({k: v for k, v in value.items() if k != field})


@pytest.mark.parametrize('index,change', [
    (0, {'requestId': ' '}), (0, {'name': ' '}), (0, {'name': 3}), (0, {'name': 'x' * 2001}),
    (0, {'location': ''}), (0, {'location': 'x' * 2001}), (0, {'isStudyRoom': 1}),
    (0, {'enabled': 'true'}), (0, {'registered': False}), (0, {'spaceId': 'injected'}),
    (0, {'ownerKind': 'OTHER'}), (0, {'departmentId': 'dep-cs'}), (0, {'ownerKind': 'DEPARTMENT'}),
    (1, {'expectedVersion': True}), (1, {'expectedVersion': 0}), (1, {'expectedVersion': '1'}),
    (1, {'spaceId': None}), (2, {'name': 'extra'}), (2, {'operation': 'DELETE_SPACE'}),
    (3, {'seatCode': ' '}), (3, {'seatCode': ' A '}), (3, {'socketTags': None}),
    (3, {'socketTags': ['FIXED', 'FIXED']}), (3, {'socketTags': ['OTHER']}), (3, {'socketTags': 'FIXED'}),
    (3, {'socketTags': [1]}), (3, {'enabled': 0}), (3, {'seatId': 'injected'}),
    (4, {'spaceId': 'move'}), (4, {'seatId': ''}), (4, {'expectedVersion': None}),
    (5, {'socketTags': []}), (5, {'requestId': None}), (5, {'userId': 'u-admin'}),
    (5, {'sourceModule': 'M1'}), (5, {'expectedVersion': -1}),
])
def test_invalid_values(index, change):
    with pytest.raises(ValidationError):
        MAINTENANCE_INPUT.validate_python(CASES[index] | change)


def test_canonical_tags_and_valid_owner():
    assert MAINTENANCE_INPUT.validate_python(CASES[3]).socketTags == ['FIXED', 'RAIL']
    assert MAINTENANCE_INPUT.validate_python(CASES[3] | {'socketTags': []}).socketTags == []
    assert MAINTENANCE_INPUT.validate_python(CASES[0] | {'ownerKind': 'DEPARTMENT', 'departmentId': 'dep-cs'}).departmentId == 'dep-cs'


@pytest.fixture
def foundation_db(isolated_db):
    seed_module.seed()
    return isolated_db


@pytest.mark.parametrize('field,value,sqlstate', [
    ('request_id', ' ', '23514'), ('input_data', [], '23514'), ('response_data', [], '23514'),
    ('input_data', None, '23514'), ('requested_by_user_id', 'missing', '23503'),
    ('requested_by_user_id', None, '23502'), ('created_at', None, '23502'),
])
def test_receipt_constraints(foundation_db, field, value, sqlstate):
    row = MasterRequest(request_id='req', input_data={}, response_data={}, requested_by_user_id='u-admin',
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    setattr(row, field, value)
    with pytest.raises(IntegrityError) as caught:
        with foundation_db.begin() as db:
            db.add(row)
    assert caught.value.orig.sqlstate == sqlstate


def test_receipt_unique_and_code_history_constraints(foundation_db):
    with foundation_db.begin() as db:
        db.add(MasterRequest(request_id='req', input_data={}, response_data={}, requested_by_user_id='u-admin',
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        db.add(SeatCodeHistory(seat_code='OLD'))
    for row, code in [
        (MasterRequest(request_id='req', input_data={}, response_data={}, requested_by_user_id='u-admin',
                       created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)), '23505'),
        (SeatCodeHistory(seat_code='OLD'), '23505'), (SeatCodeHistory(seat_code=' '), '23514'),
    ]:
        with pytest.raises(IntegrityError) as caught:
            with foundation_db.begin() as db:
                db.add(row)
        assert caught.value.orig.sqlstate == code


def test_migration_backfills_current_and_unregistered_codes_preserving_all_prior_tables(foundation_db):
    with foundation_db.begin() as db:
        db.get(Seat, 'seat-cs-1').registered = False
    def snapshot():
        with foundation_db() as db:
            return {t.name: db.execute(select(t).order_by(*t.primary_key.columns)).all()
                    for t in Base.metadata.sorted_tables if t.name not in {'master_requests', 'seat_code_history'}}
    before = snapshot()
    assert len(before) == 17
    migration = ScriptDirectory(str(ROOT / 'backend/d/migrations')).get_revision('d_b7a26ef195c4').module
    with foundation_db.begin() as db:
        assert re.fullmatch(r'd_seed_test_[0-9a-f]{32}', db.scalar(text('SELECT current_schema()')))
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.downgrade()
            migration.upgrade()
        assert set(db.scalars(select(SeatCodeHistory.seat_code))) == set(db.scalars(select(Seat.seat_code)))
        inspector = inspect(db.connection())
        for model in (MasterRequest, SeatCodeHistory):
            assert {c['name'] for c in inspector.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            assert {c['name'] for c in inspector.get_check_constraints(model.__tablename__)} == {
                c.name for c in model.__table__.constraints if c.__class__.__name__ == 'CheckConstraint'}
    assert snapshot() == before
