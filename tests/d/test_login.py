"""登录业务的真实 PostgreSQL 测试；仅操作隔离 schema、测试密钥与测试时钟。"""

from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
import pytest
from sqlalchemy import func, select

from backend.d import credentials
from backend.d import login as login_service
import backend.d.seed as seed_module
from backend.d.models import LoginSession, Role, User
from backend.shared.clock import format_time, parse_time
from backend.shared.errors import ApiError


_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def login_env(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()

    def factory(module):
        assert module == "d"
        return isolated_db

    monkeypatch.setattr(login_service, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
    clock_file = tmp_path / "clock.txt"
    clock_file.write_text(format_time(_START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock_file))
    return isolated_db, clock_file


def session_count(factory):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(LoginSession))


def test_login_persists_session_and_returns_eight_hour_expiry(login_env, fixture_data):
    factory, _ = login_env
    result = login_service.login("student1", fixture_data["password"], "login-1")
    assert set(result) == {"credential", "expiresAt", "identity"}
    assert parse_time(result["expiresAt"]) == _START + timedelta(hours=8)
    assert result["identity"]["userId"] == "u-student1"
    assert result["identity"]["roleIds"] == ["r-student"]
    assert session_count(factory) == 1

    with factory() as db:
        session = db.scalars(select(LoginSession)).one()
        assert session.user_id == "u-student1"
        assert session.login_account == "student1"
        assert session.request_id == "login-1"
        assert session.created_at == _START
        assert session.expires_at == _START + timedelta(hours=8)
        assert session.revoked_at is None
        digest_matches = session.credential_hash == credentials.credential_digest(result["credential"])
        assert digest_matches, "数据库应保存凭据摘要"
        stores_no_plain_token = session.credential_hash != result["credential"]
        assert stores_no_plain_token
        is_argon2 = session.login_password_hash.startswith("$argon2id$")
        assert is_argon2
        password_valid = PasswordHasher().verify(session.login_password_hash, fixture_data["password"])
        assert password_valid


def test_wrong_password_creates_no_session(login_env):
    factory, _ = login_env
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", "wrong-test-password", "wrong-1")
    assert caught.value.code == "AUTH_FAILED"
    assert session_count(factory) == 0


def test_unknown_account_creates_no_session(login_env, fixture_data):
    factory, _ = login_env
    with pytest.raises(ApiError) as caught:
        login_service.login("unknown-test-account", fixture_data["password"], "unknown-1")
    assert caught.value.code == "AUTH_FAILED"
    assert session_count(factory) == 0


def test_disabled_account_cannot_login(login_env, fixture_data):
    factory, _ = login_env
    with factory.begin() as db:
        db.get(User, "u-student1").account_status = "DISABLED"
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", fixture_data["password"], "disabled-1")
    assert caught.value.code == "AUTH_FAILED"
    assert session_count(factory) == 0


def test_same_request_reuses_token_without_extending_expiry(login_env, fixture_data):
    factory, clock_file = login_env
    first = login_service.login("student1", fixture_data["password"], "repeat-1")
    clock_file.write_text(format_time(_START + timedelta(hours=1)), encoding="utf-8")
    repeated = login_service.login("student1", fixture_data["password"], "repeat-1")
    same_token = repeated["credential"] == first["credential"]
    assert same_token, "重复登录应返回原凭据"
    assert repeated["expiresAt"] == first["expiresAt"]
    assert session_count(factory) == 1
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().created_at == _START


def test_same_request_with_changed_password_is_rejected(login_env, fixture_data):
    factory, _ = login_env
    login_service.login("student1", fixture_data["password"], "reuse-1")
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", "different-test-password", "reuse-1")
    assert caught.value.code == "REQUEST_ID_REUSED"
    assert session_count(factory) == 1


@pytest.mark.parametrize("seconds", [8 * 3600, 8 * 3600 + 1], ids=["at-expiry", "after-expiry"])
def test_expired_original_request_cannot_create_or_extend_session(
    login_env, fixture_data, seconds,
):
    factory, clock_file = login_env
    first = login_service.login("student1", fixture_data["password"], "expires-1")
    clock_file.write_text(format_time(_START + timedelta(seconds=seconds)), encoding="utf-8")
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", fixture_data["password"], "expires-1")
    assert caught.value.code == "AUTH_FAILED"
    assert session_count(factory) == 1
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().expires_at == parse_time(first["expiresAt"])

    renewed = login_service.login("student1", fixture_data["password"], "expires-new")
    changed = renewed["credential"] != first["credential"]
    assert changed
    assert session_count(factory) == 2


def test_original_request_is_valid_immediately_before_expiry(login_env, fixture_data):
    _, clock_file = login_env
    first = login_service.login("student1", fixture_data["password"], "boundary-1")
    clock_file.write_text(
        format_time(_START + timedelta(hours=8) - timedelta(microseconds=1)),
        encoding="utf-8",
    )
    repeated = login_service.login("student1", fixture_data["password"], "boundary-1")
    same_token = repeated["credential"] == first["credential"]
    assert same_token
    assert repeated["expiresAt"] == first["expiresAt"]


def test_revoked_original_request_cannot_revive_session(login_env, fixture_data):
    factory, _ = login_env
    login_service.login("student1", fixture_data["password"], "revoked-1")
    with factory.begin() as db:
        db.scalars(select(LoginSession)).one().revoked_at = _START
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", fixture_data["password"], "revoked-1")
    assert caught.value.code == "AUTH_FAILED"
    assert session_count(factory) == 1
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().revoked_at == _START


def test_new_request_creates_a_distinct_session(login_env, fixture_data):
    factory, _ = login_env
    first = login_service.login("student1", fixture_data["password"], "new-1")
    second = login_service.login("student1", fixture_data["password"], "new-2")
    distinct = first["credential"] != second["credential"]
    assert distinct
    assert session_count(factory) == 2


def test_request_id_is_scoped_to_account(login_env, fixture_data):
    factory, _ = login_env
    first = login_service.login("student1", fixture_data["password"], "shared-id")
    second = login_service.login("student2", fixture_data["password"], "shared-id")
    assert first["identity"]["userId"] == "u-student1"
    assert second["identity"]["userId"] == "u-student2"
    distinct = first["credential"] != second["credential"]
    assert distinct
    assert session_count(factory) == 2


def test_repeat_reads_current_identity_and_permissions(login_env, fixture_data):
    factory, _ = login_env
    first = login_service.login("student1", fixture_data["password"], "current-1")
    assert first["identity"]["grants"]
    with factory.begin() as db:
        db.get(User, "u-student1").display_name = "修改后的显示名"
        db.get(Role, "r-student").status = "DISABLED"
    repeated = login_service.login("student1", fixture_data["password"], "current-1")
    same_token = repeated["credential"] == first["credential"]
    assert same_token
    assert repeated["identity"]["displayName"] == "修改后的显示名"
    assert repeated["identity"]["roleIds"] == []
    assert repeated["identity"]["grants"] == []
    assert session_count(factory) == 1


def test_disabled_account_cannot_replay_successful_login(login_env, fixture_data):
    factory, _ = login_env
    login_service.login("student1", fixture_data["password"], "disable-replay")
    with factory.begin() as db:
        db.get(User, "u-student1").account_status = "DISABLED"
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", fixture_data["password"], "disable-replay")
    assert caught.value.code == "AUTH_FAILED"
    assert session_count(factory) == 1


def test_password_change_requires_new_request_and_current_password(login_env, fixture_data):
    factory, _ = login_env
    login_service.login("student1", fixture_data["password"], "password-change")
    new_password = "changed-password-in-isolated-test"
    with factory.begin() as db:
        db.get(User, "u-student1").password_hash = PasswordHasher().hash(new_password)
    with pytest.raises(ApiError) as changed_input:
        login_service.login("student1", new_password, "password-change")
    assert changed_input.value.code == "REQUEST_ID_REUSED"
    with pytest.raises(ApiError) as outdated_input:
        login_service.login("student1", fixture_data["password"], "password-change")
    assert outdated_input.value.code == "AUTH_FAILED"
    assert session_count(factory) == 1

    result = login_service.login("student1", new_password, "password-change-new")
    assert result["identity"]["userId"] == "u-student1"
    assert session_count(factory) == 2


def test_missing_key_does_not_persist_login(login_env, fixture_data, monkeypatch):
    factory, _ = login_env
    monkeypatch.delenv("D_SESSION_SECRET")
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", fixture_data["password"], "no-key")
    assert caught.value.code == "UNAVAILABLE"
    assert session_count(factory) == 0


def test_changed_key_does_not_return_mismatched_token(login_env, fixture_data, monkeypatch):
    factory, _ = login_env
    first = login_service.login("student1", fixture_data["password"], "changed-key")
    monkeypatch.setenv("D_SESSION_SECRET", "cd" * 32)
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", fixture_data["password"], "changed-key")
    assert caught.value.code == "AUTH_FAILED"
    assert session_count(factory) == 1
    with factory() as db:
        original_preserved = (
            db.scalars(select(LoginSession)).one().credential_hash
            == credentials.credential_digest(first["credential"])
        )
        assert original_preserved


def test_invalid_stored_password_hash_is_an_internal_error(login_env, fixture_data):
    factory, _ = login_env
    with factory.begin() as db:
        db.get(User, "u-student1").password_hash = "invalid-stored-hash"
    with pytest.raises(ApiError) as caught:
        login_service.login("student1", fixture_data["password"], "invalid-hash")
    assert caught.value.code == "INTERNAL_ERROR"
    assert session_count(factory) == 0


@pytest.mark.parametrize(
    "account,password,request_id",
    [("", "test", "id"), ("student1", "", "id"), ("student1", "test", " "), (None, "test", "id")],
    ids=["empty-account", "empty-password", "blank-request-id", "non-string"],
)
def test_invalid_input_is_rejected_before_database_access(account, password, request_id, monkeypatch):
    def unexpected_database_access(module):
        pytest.fail("无效输入不应访问数据库")

    monkeypatch.setattr(login_service, "session_factory", unexpected_database_access)
    with pytest.raises(ApiError) as caught:
        login_service.login(account, password, request_id)
    assert caught.value.code == "INVALID_ARGUMENT"
