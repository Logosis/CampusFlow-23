"""D-S12 公共设置：事务内版本检查、持久化请求结果与串行更新。"""

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.models import PublicConfig, PublicConfigRequest
from backend.d.permissions import check_permission_in_session
from backend.d.public_config_schemas import CONFIG_INPUT, PublicConfigOutput
from backend.shared.clock import utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


def _output(row):
    return {
        "authMethod": row.auth_method,
        "notificationChannel": row.notification_channel,
        "notificationRetryLimit": row.notification_retry_limit,
        "version": row.version,
    }


def manage_public_config(*, input, actor_mode, source_module, user_id=None):
    try:
        request = CONFIG_INPUT.validate_python(input)
    except ValidationError:
        raise ApiError("INVALID_ARGUMENT", "公共设置字段、类型或范围不合法") from None
    if source_module != "m1" or actor_mode != "USER":
        raise ApiError("FORBIDDEN", "公共设置必须由M1管理入口传递用户身份")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ApiError("UNAUTHENTICATED", "需要已核验的用户身份")

    updating = request.operation == "UPDATE"
    try:
        with session_factory("d").begin() as db:
            statement = select(PublicConfig).where(PublicConfig.config_id == 1)
            if updating:
                # 同一配置上的所有写入串行，重复请求在等待后读取已提交的原结果。
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                statement = statement.with_for_update()
            row = db.scalar(statement)
            decision = check_permission_in_session(
                db, permission_code="d.config.manage" if updating else "d.config.read",
                target_scope={"type": "GLOBAL", "id": None}, actor_mode="USER",
                source_module="m1", user_id=user_id,
            )
            if not decision["allowed"]:
                raise ApiError("FORBIDDEN", "需要公共设置的全局查询或管理权限")
            if row is None:
                raise ApiError("UNAVAILABLE", "公共设置尚未初始化")
            if not updating:
                result = _output(row)
            else:
                payload = request.model_dump(exclude={"requestId"})
                # 此表专用于 (m1, manage_public_config)，不同用户共用 requestId 空间。
                previous = db.get(PublicConfigRequest, request.requestId)
                if previous is not None:
                    if previous.input_data != payload:
                        raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同的公共设置输入")
                    result = PublicConfigOutput.model_validate(previous.response_data).model_dump()
                else:
                    if row.version != request.expectedVersion:
                        raise ApiError("CONFLICT", "公共设置版本已变化，请重新查询")
                    row.notification_retry_limit = request.notificationRetryLimit
                    row.version += 1
                    result = _output(row)
                    db.add(PublicConfigRequest(
                        request_id=request.requestId, input_data=payload, response_data=result,
                        requested_by_user_id=user_id, created_at=utc_now(),
                    ))
                    db.flush()
        # 离开事务且提交成功后才能确认；原结果与配置在同一事务持久化。
        return result
    except (InterfaceError, OperationalError, SQLAlchemyTimeoutError):
        raise ApiError("UNAVAILABLE", "公共设置存储不可用或提交结果未确认，请保留原请求编号核实") from None
