"""S11严格契约、新表约束、引用及无损迁移。"""
from datetime import datetime, timezone
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from pydantic import ValidationError
import pytest
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.d.audit_schemas import AUDIT_INPUT, AuditEventOutput
from backend.d.models import AuditEvent, Base, Space, User
import backend.d.seed as seed_module
from backend.shared.settings import ROOT

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
APPEND = {'operation': 'APPEND', 'requestId': 'request', 'sourceModule': 'M3', 'actorType': 'USER',
          'actorId': 'u-admin', 'objectType': 'WORK_ORDER', 'businessId': 'work', 'spaceId': 'sp-cs',
          'action': 'DISPATCH', 'occurredAt': '2026-01-01T00:00:00Z', 'result': 'SUCCESS', 'summary': '完成'}


@pytest.mark.parametrize('field', list(APPEND))
def test_append_requires_each_field(field):
    value = {k: v for k, v in APPEND.items() if k != field}
    with pytest.raises(ValidationError):
        AUDIT_INPUT.validate_python(value)


@pytest.mark.parametrize('changes', [
    {'requestId': ' '}, {'sourceModule': 'D'}, {'sourceModule': 'm3'}, {'actorType': 'other'},
    {'actorType': 'UNKNOWN', 'actorId': 'u-admin'}, {'actorType': 'USER', 'actorId': None},
    {'actorType': 'SYSTEM', 'actorId': 'm3'}, {'actorType': 'SYSTEM', 'actorId': 'M2'},
    {'summary': ''}, {'summary': 'x' * 2001}, {'action': ' '}, {'objectType': ' '},
    {'businessId': 1}, {'spaceId': ''}, {'occurredAt': '2026-01-01T00:00:00'},
    {'occurredAt': NOW}, {'result': 'PENDING'}, {'verifiedBy': 'D'}, {'userId': 'u-admin'},
])
def test_invalid_append_rejected(changes):
    with pytest.raises(ValidationError):
        AUDIT_INPUT.validate_python(APPEND | changes)


@pytest.mark.parametrize('value', [
    {'operation': 'GET'}, {'operation': 'GET', 'requestId': 'x', 'auditId': 'y'},
    {'operation': 'GET', 'requestId': None}, {'operation': 'GET', 'auditId': 'x', 'sourceModule': None},
    {'operation': 'GET', 'auditId': 'x', 'limit': 1}, {'operation': 'LIST', 'businessId': None},
    {'operation': 'LIST', 'limit': True}, {'operation': 'LIST', 'limit': '1'},
    {'operation': 'LIST', 'limit': 0}, {'operation': 'LIST', 'limit': 101},
    {'operation': 'LIST', 'cursor': None}, {'operation': 'LIST', 'cursor': ''},
    {'operation': 'LIST', 'sourceModule': 'm3'}, {'operation': 'LIST', 'result': 'SUCCESS'},
    {'operation': 'LIST', 'timeFrom': '2026-01-01T00:00:00Z', 'timeTo': '2026-01-01T00:00:00Z'},
    {'operation': 'LIST', 'timeFrom': '2026-01-02T00:00:00Z', 'timeTo': '2026-01-01T00:00:00Z'},
    {'operation': 'DELETE', 'auditId': 'x'}, {'operation': 'UPDATE', 'auditId': 'x'},
])
def test_query_rejects_null_types_extra_fields_and_bad_windows(value):
    with pytest.raises(ValidationError):
        AUDIT_INPUT.validate_python(value)


def test_default_query_and_normalized_timestamps_and_output_fields():
    query = AUDIT_INPUT.validate_python({'operation': 'LIST'})
    assert query.limit == 20 and query.cursor is None
    value = AUDIT_INPUT.validate_python(APPEND | {'occurredAt': '2026-01-01T08:00:00+08:00', 'spaceId': None})
    assert value.occurredAt == '2026-01-01T00:00:00Z' and value.spaceId is None
    result = {'auditId': 'a', **{k: v for k, v in APPEND.items() if k != 'operation'}, 'verifiedBy': 'M3'}
    assert AuditEventOutput.model_validate(result).model_dump() == result
    with pytest.raises(ValidationError):
        AuditEventOutput.model_validate(result | {'recordedAt': APPEND['occurredAt']})


@pytest.fixture
def event_db(isolated_db):
    seed_module.seed()
    return isolated_db


def row():
    return AuditEvent(audit_id='aud-1', source_module='M3', request_id='request', actor_type='USER',
                      actor_user_id='u-admin', object_type='WORK_ORDER', business_id='work', space_id='sp-cs',
                      action='DISPATCH', occurred_at=NOW, result='SUCCESS', summary='完成', verified_by='M3',
                      recorded_at=NOW, input_data={})


@pytest.mark.parametrize('field,value,code', [
    ('audit_id', '', '23514'), ('request_id', ' ', '23514'), ('source_module', 'other', '23514'),
    ('actor_type', 'other', '23514'), ('actor_user_id', None, '23514'),
    ('actor_user_id', 'missing', '23503'), ('space_id', 'missing', '23503'),
    ('object_type', '', '23514'), ('business_id', ' ', '23514'), ('action', '', '23514'),
    ('summary', 'x' * 2001, '23514'), ('result', 'PENDING', '23514'),
    ('verified_by', 'M2', '23514'), ('input_data', [], '23514'), ('occurred_at', None, '23502'),
])
def test_database_constraints(event_db, field, value, code):
    record = row()
    setattr(record, field, value)
    with pytest.raises(IntegrityError) as caught:
        with event_db.begin() as db:
            db.add(record)
    assert caught.value.orig.sqlstate == code


def test_database_unique_source_namespace_and_historical_references(event_db):
    with event_db.begin() as db:
        db.add(row())
    duplicate = row()
    duplicate.audit_id = 'aud-2'
    with pytest.raises(IntegrityError) as caught:
        with event_db.begin() as db:
            db.add(duplicate)
    assert caught.value.orig.sqlstate == '23505'
    other = row()
    other.audit_id, other.source_module, other.verified_by = 'aud-3', 'M2', 'M2'
    with event_db.begin() as db:
        db.add(other)
    for model, where in [(User, User.user_id == 'u-admin'), (Space, Space.space_id == 'sp-cs')]:
        with pytest.raises(IntegrityError) as caught:
            with event_db.begin() as db:
                db.execute(delete(model).where(where))
        assert caught.value.orig.sqlstate == '23503'


def test_migration_roundtrip_preserves_all_previous_tables_and_indexes(event_db):
    def snapshot():
        with event_db() as db:
            return {t.name: db.execute(select(t).order_by(*t.primary_key.columns)).all()
                    for t in Base.metadata.sorted_tables if t.name != 'audit_events'}
    before = snapshot()
    assert len(before) == 18
    migration = ScriptDirectory(str(ROOT / 'backend/d/migrations')).get_revision('d_d34b70ea2c91').module
    with event_db.begin() as db:
        assert re.fullmatch(r'd_seed_test_[0-9a-f]{32}', db.scalar(text('SELECT current_schema()')))
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.downgrade()
            migration.upgrade()
        inspector = inspect(db.connection())
        assert {c['name'] for c in inspector.get_columns('audit_events')} == set(AuditEvent.__table__.columns.keys())
        assert {c['name'] for c in inspector.get_check_constraints('audit_events')} == {
            c.name for c in AuditEvent.__table__.constraints if c.__class__.__name__ == 'CheckConstraint'}
        assert {i['name'] for i in inspector.get_indexes('audit_events') if not i.get('duplicates_constraint')} == {
            i.name for i in AuditEvent.__table__.indexes}
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
    assert snapshot() == before
