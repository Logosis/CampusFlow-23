"""M2 公开端点：只做输入解析、身份上下文与响应封装，业务在 service.py。"""
from fastapi import APIRouter, Request

from backend.shared.errors import ApiError, success
from backend.shared.models import SuccessResponse

from backend.m2 import schemas
from backend.m2 import service

router = APIRouter()


def caller_of(request: Request) -> service.core.Caller:
    """身份来自共同入口 guard：USER 模式已核验登录，SYSTEM 模式由模块凭据识别来源。"""
    mode = getattr(request.state, "actor_mode", None)
    if mode not in {"USER", "SYSTEM"}:
        raise ApiError("UNAUTHENTICATED", "调用模式未确定")
    identity = getattr(request.state, "identity", None)
    if mode == "SYSTEM":
        return service.core.Caller(user_id=None, token=None, mode="SYSTEM")
    if not isinstance(identity, dict) or not isinstance(identity.get("userId"), str):
        raise ApiError("UNAUTHENTICATED", "需要用户登录")
    from backend.shared.auth import bearer

    return service.core.Caller(user_id=identity["userId"], token=bearer(request), mode="USER")


@router.post("/get_allowed_actions")
async def get_allowed_actions(
    input: schemas.GetAllowedActionsInput, request: Request
) -> SuccessResponse[schemas.AllowedActionsOut]:
    return success(await service.get_allowed_actions(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/submit_application")
async def submit_application(
    input: schemas.SubmitApplicationInput, request: Request
) -> SuccessResponse[schemas.ApplicationView]:
    return success(await service.submit_application(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/get_approval_tasks")
async def get_approval_tasks(
    input: schemas.GetApprovalTasksInput, request: Request
) -> SuccessResponse[schemas.PagedApplications]:
    return success(await service.get_approval_tasks(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/submit_approval")
async def submit_approval(
    input: schemas.SubmitApprovalInput, request: Request
) -> SuccessResponse[schemas.ApplicationView]:
    return success(await service.submit_approval(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/get_application")
async def get_application(
    input: schemas.GetApplicationInput, request: Request
) -> SuccessResponse[schemas.ApplicationView]:
    return success(await service.get_application(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/get_applications")
async def get_applications(
    input: schemas.GetApplicationsInput, request: Request
) -> SuccessResponse[schemas.PagedApplications]:
    return success(await service.get_applications(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/cancel_application")
async def cancel_application(
    input: schemas.CancelApplicationInput, request: Request
) -> SuccessResponse[schemas.ApplicationView]:
    return success(await service.cancel_application(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/get_approval_config")
async def get_approval_config(
    input: schemas.GetApprovalConfigInput, request: Request
) -> SuccessResponse[schemas.ApprovalConfigOut]:
    return success(await service.get_approval_config(input.model_dump(), caller_of(request)))


@router.post("/update_approval_config")
async def update_approval_config(
    input: schemas.UpdateApprovalConfigInput, request: Request
) -> SuccessResponse[schemas.ApprovalConfigOut]:
    return success(await service.update_approval_config(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/get_maintenance_impact")
async def get_maintenance_impact(
    input: schemas.GetMaintenanceImpactInput, request: Request
) -> SuccessResponse[schemas.MaintenanceImpact]:
    return success(await service.get_maintenance_impact(input.model_dump(exclude_none=True), caller_of(request)))


@router.post("/get_classroom_options")
async def get_classroom_options(
    input: schemas.GetClassroomOptionsInput, request: Request
) -> SuccessResponse[schemas.PagedClassroomOptions]:
    return success(await service.get_classroom_options(input.model_dump(exclude_none=True), caller_of(request)))
