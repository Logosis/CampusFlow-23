from fastapi import APIRouter, Request

from backend.d.public_config import manage_public_config as perform_manage_public_config
from backend.d.public_config_schemas import PublicConfigInput, PublicConfigOutput
from backend.d.people import query_people as perform_query_people
from backend.d.people_schemas import QueryPeopleInput, QueryPeopleOutput
from backend.d.master import query_master as perform_query_master
from backend.d.master_schemas import QueryMasterInput, QueryMasterOutput
from backend.d.login import login as perform_login
from backend.d.logout import logout as perform_logout
from backend.d.permissions import check_permission as perform_check_permission
from backend.d.schemas import (
    CheckPermissionInput, CheckPermissionOutput,
    IdentityInput, IdentityOutput, LoginInput, LoginOutput, LogoutInput, LogoutOutput,
)
from backend.shared.auth import bearer
from backend.shared.errors import success
from backend.shared.models import SuccessResponse


router = APIRouter()


@router.post("/login", response_model=SuccessResponse[LoginOutput], tags=["identity"])
def login(input: LoginInput):
    return success(perform_login(
        account=input.account,
        password=input.password,
        request_id=input.requestId,
    ))


@router.post("/get_identity", response_model=SuccessResponse[IdentityOutput], tags=["identity"])
async def get_identity(input: IdentityInput, request: Request):
    # 共同 guard 已调用 D 的凭据校验函数；只返回本请求核验后的身份。
    return success(request.state.identity)


@router.post("/logout", response_model=SuccessResponse[LogoutOutput], tags=["identity"])
def logout(input: LogoutInput, request: Request):
    # 退出允许使用过期/已撤销凭据，不能调用要求当前会话有效的身份 resolver。
    return success(perform_logout(credential=bearer(request), request_id=input.requestId))


@router.post("/check_permission", response_model=SuccessResponse[CheckPermissionOutput], tags=["permissions"])
def check_permission(input: CheckPermissionInput, request: Request):
    # 身份与来源只取 guard 的核验结果，不能接收请求体自报的主体。
    mode = request.state.actor_mode
    user_id = request.state.identity["userId"] if mode == "USER" else None
    return success(perform_check_permission(
        permission_code=input.permissionCode,
        target_scope=input.targetScope.model_dump() if input.targetScope is not None else None,
        actor_mode=mode,
        source_module=request.state.source_module,
        user_id=user_id,
    ))


@router.post("/query_master", response_model=SuccessResponse[QueryMasterOutput], tags=["master"])
def query_master(input: QueryMasterInput, request: Request):
    mode = request.state.actor_mode
    return success(perform_query_master(
        input=input, actor_mode=mode, source_module=request.state.source_module,
        user_id=request.state.identity["userId"] if mode == "USER" else None,
    ))


@router.post("/query_people", response_model=SuccessResponse[QueryPeopleOutput], tags=["people"])
def query_people(input: QueryPeopleInput, request: Request):
    mode = request.state.actor_mode
    return success(perform_query_people(
        input=input, actor_mode=mode, source_module=request.state.source_module,
        user_id=request.state.identity["userId"] if mode == "USER" else None,
    ))


@router.post("/manage_public_config", response_model=SuccessResponse[PublicConfigOutput], tags=["config"])
def manage_public_config(input: PublicConfigInput, request: Request):
    return success(perform_manage_public_config(
        input=input, actor_mode=request.state.actor_mode,
        source_module=request.state.source_module, user_id=request.state.identity["userId"],
    ))
