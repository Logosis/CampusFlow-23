"""D-S05：独立PostgreSQL连接验证授权并发、版本及当前权限。"""
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

from backend.d import roles
import backend.d.seed as seed_module
from backend.d.models import Role, RoleRequest, UserRole
from backend.shared.clock import format_time
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.errors import ApiError
from backend.shared.settings import ROOT, Settings

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def role_concurrent_db(monkeypatch, tmp_path):
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

    schema = f"d_roles_test_{uuid4().hex}"
    assert re.fullmatch(r"d_roles_test_[0-9a-f]{32}", schema)
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

        for service in (seed_module, roles):
            monkeypatch.setattr(service, "session_factory", test_factory)
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
            assert re.fullmatch(r"d_roles_test_[0-9a-f]{32}", schema)
            with source_engine.begin() as cleanup:
                cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')



def request(data, user='u-admin'):
    try:
        return roles.manage_roles(input=data, actor_mode='USER', source_module='m1', user_id=user)
    except ApiError as exc:
        return exc.code


def create_input(request_id='same', name='new'):
    return {'operation': 'CREATE_ROLE', 'requestId': request_id, 'name': name, 'grants': []}


def compete(first, second):
    barrier = Barrier(2)
    def run(operation):
        barrier.wait(timeout=10)
        return operation()
    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = executor.submit(run, first), executor.submit(run, second)
        return left.result(timeout=30), right.result(timeout=30)


def test_duplicate_create_commits_one_role_and_receipt(role_concurrent_db):
    left, right = compete(lambda: request(create_input()), lambda: request(create_input()))
    assert isinstance(left, dict) and left == right
    with role_concurrent_db() as db:
        assert db.scalar(select(func.count()).select_from(RoleRequest)) == 1
        assert db.scalar(select(func.count()).select_from(Role).where(Role.name == 'new')) == 1


def test_changed_create_same_request_has_one_winner(role_concurrent_db):
    result = compete(lambda: request(create_input()), lambda: request(create_input(name='other')))
    assert len([r for r in result if isinstance(r, dict)]) == 1
    assert [r for r in result if isinstance(r, str)] == ['REQUEST_ID_REUSED']


def test_update_version_competition_has_one_winner(role_concurrent_db):
    role = request(create_input())
    data = {'operation': 'UPDATE_ROLE', 'roleId': role['roleId'], 'expectedVersion': 1,
            'requestId': 'a', 'name': 'A', 'grants': [], 'status': 'ACTIVE'}
    result = compete(lambda: request(data), lambda: request(data | {'requestId': 'b', 'name': 'B'}))
    assert len([r for r in result if isinstance(r, dict) and r['version'] == 2]) == 1
    assert [r for r in result if isinstance(r, str)] == ['CONFLICT']


def test_assignment_version_competition_has_one_winner(role_concurrent_db):
    data = {'operation': 'SET_USER_ROLES', 'userId': 'u-student1', 'expectedVersion': 1,
            'requestId': 'a', 'roleIds': []}
    result = compete(lambda: request(data), lambda: request(data | {'requestId': 'b', 'roleIds': ['r-student']}))
    assert len([r for r in result if isinstance(r, dict) and r['version'] == 2]) == 1
    assert [r for r in result if isinstance(r, str)] == ['CONFLICT']


def test_disable_and_new_assignment_have_serializable_outcomes(role_concurrent_db):
    role = request(create_input())
    disable = {'operation': 'DISABLE_ROLE', 'roleId': role['roleId'], 'expectedVersion': 1, 'requestId': 'disable'}
    assign = {'operation': 'SET_USER_ROLES', 'userId': 'u-student1', 'expectedVersion': 1,
              'roleIds': [role['roleId']], 'requestId': 'assign'}
    disabled, assigned = compete(lambda: request(disable), lambda: request(assign))
    assert disabled['status'] == 'DISABLED'
    with role_concurrent_db() as db:
        assert db.get(Role, role['roleId']).status == 'DISABLED'
        association = db.get(UserRole, ('u-student1', role['roleId']))
        if isinstance(assigned, dict):
            assert association is not None
        else:
            assert assigned == 'CONFLICT' and association is None


def test_revoked_admin_cannot_write_after_waiting_for_mutation_lock(role_concurrent_db):
    # 主连接持有业务同一把事务锁并撤销权限；等待者拿锁后必须看到撤销结果。
    started = Barrier(2)
    def waiting():
        started.wait(timeout=10)
        return request(create_input('blocked'))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with role_concurrent_db.begin() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': roles.LOCK_ID})
            db.execute(delete(UserRole).where(UserRole.user_id == 'u-admin'))
            future = executor.submit(waiting)
            started.wait(timeout=10)
        assert future.result(timeout=30) == 'FORBIDDEN'
    with role_concurrent_db() as db:
        assert db.get(RoleRequest, 'blocked') is None


def test_shared_request_namespace_across_mutation_operations(role_concurrent_db):
    disable = {'operation': 'DISABLE_ROLE', 'requestId': 'same', 'roleId': 'r-student', 'expectedVersion': 1}
    result = compete(lambda: request(create_input()), lambda: request(disable))
    assert len([r for r in result if isinstance(r, dict)]) == 1
    assert [r for r in result if isinstance(r, str)] == ['REQUEST_ID_REUSED']
