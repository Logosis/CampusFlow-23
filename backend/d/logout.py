"""D-S03：幂等撤销当前凭据对应的会话。"""

from sqlalchemy import func, update
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.credentials import credential_digest
from backend.d.models import LoginSession
from backend.shared.clock import utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


def logout(credential: str, request_id: str) -> dict:
    """事务提交后才确认退出；不要求原会话仍有效，也不撤销其他会话。"""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ApiError("INVALID_ARGUMENT", "退出请求编号必须为非空白字符串")
    digest = credential_digest(credential)
    try:
        with session_factory("d").begin() as db:
            # 唯一摘要限定当前会话，条件更新让并发及重复退出只记录首次撤销。
            # 即使测试时钟早于创建时间，也永久撤销，且保持表的时间约束。
            db.execute(
                update(LoginSession)
                .where(
                    LoginSession.credential_hash == digest,
                    LoginSession.revoked_at.is_(None),
                )
                .values(revoked_at=func.greatest(LoginSession.created_at, utc_now()))
                .execution_options(synchronize_session=False)
            )
        # 输入只有 requestId，无可变业务内容；按凭据的单向撤销自然满足重复语义。
        # 未知但格式正确的凭据同样确认退出，不创建会话或泄露是否存在。
        return {"loggedOut": True}
    except (OperationalError, SQLAlchemyTimeoutError):
        raise ApiError("UNAVAILABLE", "退出结果未确认，请保留原请求编号重试") from None
