"""D-S01 登录业务；HTTP 路由在后续步骤接入。"""

from datetime import timedelta
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select

from backend.d.credentials import credential_digest, make_credential, new_session_id
from backend.d.identity import build_identity
from backend.d.models import LoginSession, User
from backend.shared.clock import format_time, utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


_PASSWORD_HASHER = PasswordHasher()
_SESSION_LIFETIME = timedelta(hours=8)


def _password_matches(stored_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(stored_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError):
        raise ApiError("INTERNAL_ERROR", "账号认证数据异常") from None


def login(account: str, password: str, request_id: str) -> dict:
    """核验账号，原子保存一次成功登录，返回 D-S01 的 data。"""
    if (
        any(not isinstance(value, str) or not value for value in (account, password, request_id))
        or not account.strip()
        or not request_id.strip()
    ):
        raise ApiError("INVALID_ARGUMENT", "登录账号、密码和请求编号必须为有效字符串")

    with session_factory("d").begin() as db:
        # 同一用户的新登录和重复登录串行检查；账号与 requestId 的唯一约束另作兜底。
        user = db.scalar(
            select(User).where(User.account == account).with_for_update()
        )
        if user is None or user.account_status != "ACTIVE":
            raise ApiError("AUTH_FAILED", "账号或密码错误，或账号不可登录")

        previous = db.scalar(
            select(LoginSession)
            .where(
                LoginSession.login_account == account,
                LoginSession.request_id == request_id,
            )
            .with_for_update()
        )
        if previous is not None:
            # 用原密码哈希核对输入是否变化，不能只核对用户当前的新密码。
            if not _password_matches(previous.login_password_hash, password):
                raise ApiError("REQUEST_ID_REUSED", "登录请求编号已用于不同的输入")
            if previous.user_id != user.user_id:
                raise ApiError("AUTH_FAILED", "原登录凭据已失效，请使用新的请求编号重新登录")

            if (
                previous.login_password_hash != user.password_hash
                and not _password_matches(user.password_hash, password)
            ):
                raise ApiError("AUTH_FAILED", "账号或密码错误，或账号不可登录")

            now = utc_now()
            if (
                previous.revoked_at is not None
                or now < previous.created_at
                or now >= previous.expires_at
            ):
                raise ApiError("AUTH_FAILED", "原登录凭据已失效，请使用新的请求编号重新登录")

            token = make_credential(previous.session_id)
            if not hmac.compare_digest(credential_digest(token), previous.credential_hash):
                raise ApiError("AUTH_FAILED", "原登录凭据已失效，请使用新的请求编号重新登录")
            expires_at = previous.expires_at
        else:
            if not _password_matches(user.password_hash, password):
                raise ApiError("AUTH_FAILED", "账号或密码错误，或账号不可登录")

            now = utc_now()
            expires_at = now + _SESSION_LIFETIME
            session_id = new_session_id()
            token = make_credential(session_id)
            db.add(LoginSession(
                session_id=session_id,
                user_id=user.user_id,
                login_account=account,
                request_id=request_id,
                # 保存已有 Argon2 哈希快照，不保存明文密码。
                login_password_hash=user.password_hash,
                credential_hash=credential_digest(token),
                created_at=now,
                expires_at=expires_at,
                revoked_at=None,
            ))

        # 即使原登录请求重试，也读取当前角色与授权，不缓存旧权限。
        result = {
            "credential": token,
            "expiresAt": format_time(expires_at),
            "identity": build_identity(db, user.user_id),
        }
        db.flush()

    # 事务成功提交之后才能返回凭据。
    return result
