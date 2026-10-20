from fastapi import APIRouter, Request

from backend.d.login import login as perform_login
from backend.d.logout import logout as perform_logout
from backend.d.schemas import (
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
