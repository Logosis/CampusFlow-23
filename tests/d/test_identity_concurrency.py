"""真实 PostgreSQL 独立连接竞争：登录幂等、重复退出、退出与旧登录重试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import re
from threading import Barrier
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.d import credentials, sessions
from backend.d import login as login_service
from backend.d import logout as logout_service
import backend.d.seed as seed_module
from backend.d.models import LoginSession
from backend.shared.clock import format_time
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.errors import ApiError
from backend.shared.settings import ROOT, Settings


_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def concurrent_db(monkeypatch, tmp_path):
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

    schema = f"d_concurrency_test_{uuid4().hex}"
    assert re.fullmatch(r"d_concurrency_test_[0-9a-f]{32}", schema)
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

        for service in (seed_module, login_service, logout_service, sessions):
            monkeypatch.setattr(service, "session_factory", test_factory)
        monkeypatch.setattr(credentials, "ROOT", tmp_path)
        monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
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
            assert re.fullmatch(r"d_concurrency_test_[0-9a-f]{32}", schema)
            with source_engine.begin() as cleanup:
                cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


def compete(first, second):
    barrier = Barrier(2)

    def run(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=2) as executor:
        left = executor.submit(run, first)
        right = executor.submit(run, second)
        return left.result(timeout=30), right.result(timeout=30)


def login(fixture_data):
    return login_service.login("student1", fixture_data["password"], "concurrent-login-1")


def test_concurrent_duplicate_login_commits_only_one_session(concurrent_db, fixture_data):
    first, second = compete(lambda: login(fixture_data), lambda: login(fixture_data))
    same_credential = first["credential"] == second["credential"]
    assert same_credential
    assert first["expiresAt"] == second["expiresAt"]
    with concurrent_db() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        record = db.scalars(select(LoginSession)).one()
        assert record.expires_at == _START + timedelta(hours=8)
        assert record.revoked_at is None


def test_concurrent_duplicate_logout_leaves_one_revocation(concurrent_db, fixture_data):
    original = login(fixture_data)
    first, second = compete(
        lambda: logout_service.logout(original["credential"], "concurrent-logout-1"),
        lambda: logout_service.logout(original["credential"], "concurrent-logout-1"),
    )
    assert first == second == {"loggedOut": True}
    with concurrent_db() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        assert db.scalars(select(LoginSession)).one().revoked_at == _START
    with pytest.raises(ApiError) as caught:
        sessions.get_identity_for_credential(original["credential"])
    assert caught.value.code == "UNAUTHENTICATED"


def test_logout_racing_original_login_retry_cannot_restore_session(concurrent_db, fixture_data):
    original = login(fixture_data)

    def retry_login():
        try:
            result = login(fixture_data)
        except ApiError as exc:
            assert exc.code == "AUTH_FAILED"
            return "rejected"
        same_credential = result["credential"] == original["credential"]
        assert same_credential
        return "completed-before-revocation"

    retry_result, logout_result = compete(
        retry_login,
        lambda: logout_service.logout(original["credential"], "concurrent-logout-1"),
    )
    assert retry_result in {"rejected", "completed-before-revocation"}
    assert logout_result == {"loggedOut": True}
    with concurrent_db() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        assert db.scalars(select(LoginSession)).one().revoked_at == _START
    with pytest.raises(ApiError) as caught:
        sessions.get_identity_for_credential(original["credential"])
    assert caught.value.code == "UNAUTHENTICATED"
    with pytest.raises(ApiError) as caught:
        login(fixture_data)
    assert caught.value.code == "AUTH_FAILED"
