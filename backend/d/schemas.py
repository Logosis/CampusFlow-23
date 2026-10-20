"""D 登录与身份的公开输入输出格式。"""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from backend.shared.models import Id, InputModel, IsoTime, OutputModel


class LoginInput(InputModel):
    account: Id
    password: Annotated[
        str,
        Field(
            min_length=1,
            repr=False,
            json_schema_extra={"format": "password", "writeOnly": True},
        ),
    ]
    requestId: Id


class IdentityInput(InputModel):
    """仅接受空对象；当前用户只能由请求凭据确定。"""


class LogoutInput(InputModel):
    requestId: Id

    @field_validator("requestId")
    @classmethod
    def validate_request_id(cls, value):
        if not value.strip():
            raise ValueError("requestId must not be blank")
        return value


class LogoutOutput(OutputModel):
    loggedOut: bool


class ScopeOutput(OutputModel):
    type: Literal["GLOBAL", "DEPARTMENT", "SPACE"]
    id: Id | None

    @model_validator(mode="after")
    def validate_scope_id(self):
        if self.type == "GLOBAL" and self.id is not None:
            raise ValueError("GLOBAL scope must have a null id")
        if self.type != "GLOBAL" and (self.id is None or not self.id.strip()):
            raise ValueError("DEPARTMENT and SPACE scopes require an id")
        return self


class GrantOutput(OutputModel):
    permissionCode: Id
    scope: ScopeOutput


class IdentityOutput(OutputModel):
    userId: Id
    displayName: str
    departmentId: Id | None
    accountStatus: Literal["ACTIVE", "DISABLED"]
    roleIds: list[Id]
    grants: list[GrantOutput]


class LoginOutput(OutputModel):
    credential: Id
    expiresAt: IsoTime
    identity: IdentityOutput
