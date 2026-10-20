"""D 内部的会话编号、登录凭据及摘要工具。"""

import hashlib
import hmac
import os
import re
import secrets

from dotenv import load_dotenv

from backend.shared.errors import ApiError
from backend.shared.settings import ROOT


_HEX_64 = re.compile(r"[0-9a-f]{64}")
_TOKEN_CONTEXT = b"campusflow:d:session:v1:"


def _session_key() -> bytes:
    """读取固定密钥；配置缺失时拒绝生成凭据。"""
    load_dotenv(ROOT / ".env", override=False)
    value = os.getenv("D_SESSION_SECRET", "")
    if _HEX_64.fullmatch(value) is None:
        raise ApiError("UNAVAILABLE", "D 登录密钥未配置或格式不正确")
    return bytes.fromhex(value)


def new_session_id() -> str:
    """由服务端为一次新登录生成随机会话编号。"""
    return secrets.token_hex(32)


def make_credential(session_id: str) -> str:
    """相同密钥和会话编号生成相同凭据，供原登录请求重试使用。"""
    if not isinstance(session_id, str) or _HEX_64.fullmatch(session_id) is None:
        raise ValueError("Invalid internal session ID")
    message = _TOKEN_CONTEXT + session_id.encode("ascii")
    return hmac.new(_session_key(), message, hashlib.sha256).hexdigest()


def credential_digest(credential: str) -> str:
    """计算数据库保存/查询使用的摘要；此函数不判断会话是否有效。"""
    if not isinstance(credential, str) or _HEX_64.fullmatch(credential) is None:
        raise ApiError("UNAUTHENTICATED", "用户凭据格式不合法")
    return hashlib.sha256(credential.encode("ascii")).hexdigest()
