"""签名游标的纯函数测试，不使用开发密钥或数据库。"""

import base64
import hashlib
import hmac

import pytest

from backend.d import credentials, pagination
from backend.shared.errors import ApiError


CONTEXT = {"query": {"operation": "LIST_SEATS", "limit": 1}, "subjectId": "m1"}


@pytest.fixture(autouse=True)
def private_test_key(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "cd" * 32)


def test_cursor_roundtrip_and_no_secret():
    token = pagination.encode_cursor("seat-中文", CONTEXT)
    assert pagination.decode_cursor(token, CONTEXT) == "seat-中文"
    assert pagination.decode_cursor(None, CONTEXT) is None
    assert "cd" * 32 not in token


@pytest.mark.parametrize("cursor", ["", " ", "abc", "a.b", "中文." + "a" * 64, "." + "a" * 64, pytest.param("a" * 8193, id="too-long"), 1, [], "YWJj." + "0" * 64])
def test_invalid_cursor_rejected(cursor):
    with pytest.raises(ApiError) as error:
        pagination.decode_cursor(cursor, CONTEXT)
    assert error.value.code == "INVALID_ARGUMENT"


def test_tampering_rejected():
    token = pagination.encode_cursor("seat-1", CONTEXT)
    body, signature = token.split(".")
    forged = body + "." + ("0" if signature[0] != "0" else "1") + signature[1:]
    with pytest.raises(ApiError) as error:
        pagination.decode_cursor(forged, CONTEXT)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("context", [
    {"query": {"operation": "LIST_SPACES", "limit": 1}, "subjectId": "m1"},
    {"query": {"operation": "LIST_SEATS", "limit": 2}, "subjectId": "m1"},
    {"query": {"operation": "LIST_SEATS", "limit": 1}, "subjectId": "m2"},
])
def test_cursor_bound_to_query_and_subject(context):
    token = pagination.encode_cursor("seat-1", CONTEXT)
    with pytest.raises(ApiError) as error:
        pagination.decode_cursor(token, context)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("payload", [
    b"not-json", b"[]", b"{}",
    b'{"v":true,"after":"seat-1","binding":"bad"}',
    b'{"v":1,"after":null,"binding":"bad"}',
])
def test_signed_malformed_payload_rejected(payload):
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(bytes.fromhex("cd" * 32), pagination._CONTEXT + body, hashlib.sha256).hexdigest()
    with pytest.raises(ApiError) as error:
        pagination.decode_cursor(body.decode() + "." + signature, CONTEXT)
    assert error.value.code == "INVALID_ARGUMENT"


def test_session_credential_cannot_be_used_as_cursor():
    credential = credentials.make_credential("e" * 64)
    with pytest.raises(ApiError) as error:
        pagination.decode_cursor(credential, CONTEXT)
    assert error.value.code == "INVALID_ARGUMENT"


def test_key_rotation_invalidates_cursor(monkeypatch):
    token = pagination.encode_cursor("seat-1", CONTEXT)
    monkeypatch.setenv("D_SESSION_SECRET", "ef" * 32)
    with pytest.raises(ApiError) as error:
        pagination.decode_cursor(token, CONTEXT)
    assert error.value.code == "INVALID_ARGUMENT"


def test_missing_key_reports_unavailable(monkeypatch):
    token = pagination.encode_cursor("seat-1", CONTEXT)
    monkeypatch.delenv("D_SESSION_SECRET")
    with pytest.raises(ApiError) as error:
        pagination.decode_cursor(token, CONTEXT)
    assert error.value.code == "UNAVAILABLE"
