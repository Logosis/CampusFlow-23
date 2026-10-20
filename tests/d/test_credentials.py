"""凭据工具测试：仅使用临时配置，不读取开发者的真实密钥。"""

import re

import pytest

from backend.d import credentials
from backend.shared.errors import ApiError


@pytest.fixture(autouse=True)
def isolated_key(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
    return tmp_path


def test_new_sessions_have_distinct_opaque_credentials():
    first_id = credentials.new_session_id()
    second_id = credentials.new_session_id()
    assert first_id != second_id
    assert re.fullmatch(r"[0-9a-f]{64}", first_id)
    assert re.fullmatch(r"[0-9a-f]{64}", second_id)

    first = credentials.make_credential(first_id)
    second = credentials.make_credential(second_id)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert re.fullmatch(r"[0-9a-f]{64}", second)
    assert first != second
    assert first != first_id
    assert second != second_id


def test_same_session_returns_same_credential():
    session_id = credentials.new_session_id()
    first = credentials.make_credential(session_id)
    assert credentials.make_credential(session_id) == first


def test_changing_key_changes_generated_credential(monkeypatch):
    session_id = credentials.new_session_id()
    original = credentials.make_credential(session_id)
    monkeypatch.setenv("D_SESSION_SECRET", "cd" * 32)
    assert credentials.make_credential(session_id) != original


def test_digest_is_stable_and_tampering_changes_lookup():
    token = credentials.make_credential(credentials.new_session_id())
    digest = credentials.credential_digest(token)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest != token
    assert credentials.credential_digest(token) == digest
    replacement = "0" if token[0] != "0" else "1"
    changed = replacement + token[1:]
    assert credentials.credential_digest(changed) != digest


@pytest.mark.parametrize(
    "value",
    [None, "", "ab" * 31, "not-a-hex-key-" * 5],
    ids=["missing", "empty", "short", "non-hex"],
)
def test_missing_or_invalid_key_fails_closed(value, monkeypatch):
    if value is None:
        monkeypatch.delenv("D_SESSION_SECRET")
    else:
        monkeypatch.setenv("D_SESSION_SECRET", value)
    with pytest.raises(ApiError) as caught:
        credentials.make_credential(credentials.new_session_id())
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.message == "D 登录密钥未配置或格式不正确"


@pytest.mark.parametrize(
    "value",
    [None, "", "a" * 63, "g" * 64, "用" * 64, " " + "a" * 64],
    ids=["missing", "empty", "short", "non-hex", "unicode", "leading-space"],
)
def test_malformed_credential_is_rejected(value):
    with pytest.raises(ApiError) as caught:
        credentials.credential_digest(value)
    assert caught.value.code == "UNAUTHENTICATED"
    assert caught.value.message == "用户凭据格式不合法"


def test_fixed_key_can_be_loaded_again_from_env_file(isolated_key, monkeypatch):
    (isolated_key / ".env").write_text(
        "D_SESSION_SECRET=" + "ef" * 32 + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("D_SESSION_SECRET")
    session_id = credentials.new_session_id()
    first = credentials.make_credential(session_id)

    # 移除当前进程中的值后重新读取同一配置，模拟重启后的密钥加载。
    monkeypatch.delenv("D_SESSION_SECRET")
    assert credentials.make_credential(session_id) == first


def test_env_file_does_not_override_process_configuration(isolated_key):
    session_id = credentials.new_session_id()
    expected = credentials.make_credential(session_id)
    (isolated_key / ".env").write_text(
        "D_SESSION_SECRET=" + "ef" * 32 + "\n",
        encoding="utf-8",
    )
    assert credentials.make_credential(session_id) == expected
