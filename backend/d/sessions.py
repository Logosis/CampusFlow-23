"""D 会话认证：按凭据摘要查库，返回当前有效身份。"""

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError
from starlette.concurrency import run_in_threadpool

from backend.d.credentials import credential_digest
from backend.d.identity import build_identity
from backend.d.models import LoginSession
from backend.shared.clock import utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


def get_identity_for_credential(credential: str) -> dict:
    """同步数据库工作；不缓存身份，不刷新或延长会话。"""
    digest = credential_digest(credential)
    try:
        with session_factory("d")() as db:
            session = db.scalar(
                select(LoginSession).where(LoginSession.credential_hash == digest)
            )
            if session is None:
                raise ApiError("UNAUTHENTICATED", "用户凭据无效或已失效")

            now = utc_now()
            if (
                session.revoked_at is not None
                or now < session.created_at
                or now >= session.expires_at
            ):
                raise ApiError("UNAUTHENTICATED", "用户凭据无效或已失效")

            # 同一个会话每次都重新读取用户、有效角色和授权。
            return build_identity(db, session.user_id)
    except (OperationalError, SQLAlchemyTimeoutError):
        # 不把数据库故障伪装成空身份，也不暴露连接配置或原始异常。
        raise ApiError("UNAVAILABLE", "身份存储暂时不可用，请稍后重试") from None


async def resolve_identity(credential: str) -> dict:
    """接入共同异步 guard；同步数据库访问在工作线程中执行。"""
    return await run_in_threadpool(get_identity_for_credential, credential)
