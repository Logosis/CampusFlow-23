"""D-S07：独立PostgreSQL连接验证占号、父子注销、版本及权限竞争。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
from threading import Barrier
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import delete, text, create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.d import master_maintenance as service
from backend.d import roles
import backend.d.seed as seed_module
from backend.d.models import MasterRequest, Seat, SeatCodeHistory, Space, UserRole
from backend.shared.clock import format_time
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.errors import ApiError
from backend.shared.settings import ROOT, Settings

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def maintenance_concurrent_db(monkeypatch, tmp_path):
    """仅本用例随机 schema 提交数据，让不同连接可见；结束后清理该 schema。"""
    if Settings.load("d").app_env != "test":
        pytest.fail("Concurrency checks require APP_ENV=test.", pytrace=False)
    with shared_session_factory("d")() as db:
        source_engine = db.get_bind()
    if source_engine.dialect.name != "postgresql":
        pytest.fail("Concurrency checks require PostgreSQL.", pytrace=False)

    connection_error = None
    try:
        connection = source_engine.connect()
    except SQLAlchemyError as exc:
        connection_error = type(exc).__name__
    if connection_error is not None:
        pytest.fail(f"D test database connection failed ({connection_error}).", pytrace=False)

    schema = f"d_master_test_{uuid4().hex}"
    assert re.fullmatch(r"d_master_test_[0-9a-f]{32}", schema)
    test_engine = None
    created = False
    try:
        with connection, connection.begin():
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
            scripts = ScriptDirectory(str(ROOT / "backend" / "d" / "migrations"))
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                for revision in reversed(list(scripts.walk_revisions())):
                    revision.module.upgrade()
        created = True

        # 每个工作线程取得自己的连接/事务，只能在随机测试 schema 中访问业务表。
        test_engine = create_engine(
            source_engine.url,
            connect_args={
                "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=10000",
                "connect_timeout": 5,
            },
            pool_size=3,
            max_overflow=0,
            pool_timeout=10,
        )
        with test_engine.connect() as check:
            assert check.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
        factory = sessionmaker(bind=test_engine, expire_on_commit=False)

        def test_factory(module):
            assert module == "d"
            return factory

        for target_module in (seed_module, service):
            monkeypatch.setattr(target_module, "session_factory", test_factory)
        clock_file = tmp_path / "clock.txt"
        clock_file.write_text(format_time(_START), encoding="utf-8")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("TEST_CLOCK_FILE", str(clock_file))
        seed_module.seed()
        yield factory
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if created:
            # 名称仅来自本 fixture 的随机值，绝不清空 public 或其他 schema。
            assert re.fullmatch(r"d_master_test_[0-9a-f]{32}", schema)
            with source_engine.begin() as cleanup:
                cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')



def request(data):
    try:
        return service.maintain_master(input=data, actor_mode='USER', source_module='m1', user_id='u-admin')
    except ApiError as exc:
        return exc.code


def space_input(request_id='same'):
    return {'operation': 'CREATE_SPACE', 'requestId': request_id, 'name': 'New', 'location': 'Room',
            'isStudyRoom': True, 'ownerKind': 'SCHOOL', 'departmentId': None, 'enabled': True}


def seat_input(request_id='seat', space_id='sp-school', code='NEW-CODE'):
    return {'operation': 'CREATE_SEAT', 'requestId': request_id, 'spaceId': space_id,
            'seatCode': code, 'enabled': True, 'socketTags': []}


def compete(first, second):
    barrier = Barrier(2)
    def run(action):
        barrier.wait(timeout=10)
        return action()
    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = executor.submit(run, first), executor.submit(run, second)
        return left.result(timeout=30), right.result(timeout=30)


def one_winner(results, error):
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert [r for r in results if isinstance(r, str)] == [error]


@pytest.mark.parametrize('data', [space_input(), seat_input()])
def test_same_request_commits_only_once(maintenance_concurrent_db, data):
    left, right = compete(lambda: request(data), lambda: request(data))
    assert isinstance(left, dict) and left == right
    with maintenance_concurrent_db() as db:
        assert db.scalar(select(func.count()).select_from(MasterRequest)) == 1
        if data['operation'] == 'CREATE_SEAT':
            assert db.scalar(select(func.count()).select_from(SeatCodeHistory)) == 1


def test_changed_input_same_request_has_one_winner(maintenance_concurrent_db):
    results = compete(lambda: request(space_input()), lambda: request(space_input() | {'name': 'Changed'}))
    one_winner(results, 'REQUEST_ID_REUSED')


def test_six_operations_share_one_request_namespace(maintenance_concurrent_db):
    results = compete(lambda: request(space_input()), lambda: request(seat_input(request_id='same')))
    one_winner(results, 'REQUEST_ID_REUSED')


def test_same_seat_code_competes_globally_across_spaces(maintenance_concurrent_db):
    results = compete(lambda: request(seat_input('left')), lambda: request(seat_input('right', 'sp-cs')))
    one_winner(results, 'CONFLICT')
    with maintenance_concurrent_db() as db:
        assert db.scalar(select(func.count()).select_from(Seat).where(Seat.seat_code == 'NEW-CODE')) == 1
        assert db.scalar(select(func.count()).select_from(MasterRequest)) == 1


def test_space_version_competition_has_one_winner(maintenance_concurrent_db):
    base = space_input() | {'operation': 'UPDATE_SPACE', 'spaceId': 'sp-school', 'expectedVersion': 1}
    results = compete(lambda: request(base | {'requestId': 'left'}), lambda: request(base | {'requestId': 'right', 'name': 'Other'}))
    one_winner(results, 'CONFLICT')
    with maintenance_concurrent_db() as db:
        assert db.get(Space, 'sp-school').version == 2


def test_space_unregister_and_seat_create_cannot_both_succeed(maintenance_concurrent_db):
    space = request(space_input())
    close = {'operation': 'UNREGISTER_SPACE', 'requestId': 'close', 'spaceId': space['spaceId'], 'expectedVersion': 1}
    left, right = compete(lambda: request(close), lambda: request(seat_input(space_id=space['spaceId'])))
    one_winner((left, right), 'CONFLICT')
    with maintenance_concurrent_db() as db:
        parent = db.get(Space, space['spaceId'])
        children = db.scalars(select(Seat).where(Seat.space_id == parent.space_id)).all()
        assert (parent.registered and len(children) == 1) or (not parent.registered and children == [])


def test_rename_competes_with_create_and_does_not_release_old_code(maintenance_concurrent_db):
    update = {'operation': 'UPDATE_SEAT', 'requestId': 'rename', 'seatId': 'seat-school-1', 'expectedVersion': 1,
              'seatCode': 'NEW-CODE', 'enabled': True, 'socketTags': []}
    results = compete(lambda: request(update), lambda: request(seat_input()))
    one_winner(results, 'CONFLICT')
    assert request(seat_input('reuse', code='TEST-S-001')) == 'CONFLICT'
    with maintenance_concurrent_db() as db:
        assert db.scalar(select(func.count()).select_from(Seat).where(Seat.seat_code == 'NEW-CODE')) == 1


def test_update_and_unregister_same_version_have_one_winner(maintenance_concurrent_db):
    update = {'operation': 'UPDATE_SEAT', 'requestId': 'edit', 'seatId': 'seat-school-1', 'expectedVersion': 1,
              'seatCode': 'NEW-CODE', 'enabled': True, 'socketTags': []}
    close = {'operation': 'UNREGISTER_SEAT', 'requestId': 'close', 'seatId': 'seat-school-1', 'expectedVersion': 1}
    one_winner(compete(lambda: request(update), lambda: request(close)), 'CONFLICT')
    with maintenance_concurrent_db() as db:
        assert db.get(Seat, 'seat-school-1').version == 2
        assert db.scalar(select(func.count()).select_from(MasterRequest)) == 1


@pytest.mark.parametrize('lock_key', [service.LOCK_ID, roles.LOCK_ID])
def test_waiting_maintenance_checks_permission_after_revocation(maintenance_concurrent_db, lock_key):
    started = Barrier(2)
    def waiting():
        started.wait(timeout=10)
        return request(space_input('blocked'))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with maintenance_concurrent_db.begin() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
            db.execute(delete(UserRole).where(UserRole.user_id == 'u-admin'))
            future = executor.submit(waiting)
            started.wait(timeout=10)
        assert future.result(timeout=30) == 'FORBIDDEN'
    with maintenance_concurrent_db() as db:
        assert db.get(MasterRequest, 'blocked') is None
