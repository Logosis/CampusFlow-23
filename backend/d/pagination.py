"""D 内部签名游标：绑定查询、调用主体与当前有效范围，不保存会话凭据。"""

import base64
import hashlib
import hmac
import json
import re

from backend.d.credentials import _session_key
from backend.shared.errors import ApiError


_CONTEXT = b"campusflow:d:pagination:v1:"
_FORMAT = re.compile(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}\Z")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _binding(context):
    return hashlib.sha256(_json(context)).hexdigest()


def encode_cursor(last_id, context):
    body = base64.urlsafe_b64encode(_json({"v": 1, "after": last_id, "binding": _binding(context)})).rstrip(b"=")
    signature = hmac.new(_session_key(), _CONTEXT + body, hashlib.sha256).hexdigest()
    return body.decode("ascii") + "." + signature


def decode_cursor(cursor, context):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or len(cursor) > 8192 or _FORMAT.fullmatch(cursor) is None:
        raise ApiError("INVALID_ARGUMENT", "分页游标无效，请从第一页重新查询")
    body, signature = cursor.split(".")
    expected = hmac.new(_session_key(), _CONTEXT + body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ApiError("INVALID_ARGUMENT", "分页游标无效，请从第一页重新查询")
    try:
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if (
            not isinstance(value, dict) or set(value) != {"v", "after", "binding"}
            or type(value["v"]) is not int or value["v"] != 1
            or not isinstance(value["after"], str) or not value["after"].strip()
            or value["binding"] != _binding(context)
        ):
            raise ValueError("Invalid cursor payload")
    except (ValueError, UnicodeError):
        raise ApiError("INVALID_ARGUMENT", "分页条件或权限已改变，请从第一页重新查询") from None
    return value["after"]
