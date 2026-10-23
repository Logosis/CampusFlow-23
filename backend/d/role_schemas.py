"""D-S05：九种操作、完整集合替换及严格输入输出。"""
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator

from backend.d.master_schemas import Cursor, MasterId
from backend.d.schemas import GrantOutput, ScopeInput
from backend.shared.models import InputModel, OutputModel, PositiveInt

Status = Literal['ACTIVE', 'DISABLED']


class GrantInput(InputModel):
    permissionCode: MasterId
    scope: ScopeInput


class PageInput(InputModel):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Cursor = Field(default=None)


class ListPermissionsInput(PageInput):
    operation: Literal['LIST_PERMISSIONS']


class ListUsersInput(PageInput):
    operation: Literal['LIST_USERS']
    departmentId: MasterId = Field(default=None)
    accountStatus: Status = Field(default=None)


class ListRolesInput(PageInput):
    operation: Literal['LIST_ROLES']
    status: Status = Field(default=None)


class GetRoleInput(InputModel):
    operation: Literal['GET_ROLE']
    roleId: MasterId


class GetUserRolesInput(InputModel):
    operation: Literal['GET_USER_ROLES']
    userId: MasterId


class RoleFields(InputModel):
    name: MasterId
    grants: list[GrantInput]

    @field_validator('grants')
    @classmethod
    def canonical_grants(cls, value):
        keys = [(g.permissionCode, g.scope.type, g.scope.id or '') for g in value]
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate grants are not allowed')
        return [grant for _, grant in sorted(zip(keys, value), key=lambda pair: pair[0])]


class CreateRoleInput(RoleFields):
    operation: Literal['CREATE_ROLE']
    requestId: MasterId


class UpdateRoleInput(RoleFields):
    operation: Literal['UPDATE_ROLE']
    requestId: MasterId
    expectedVersion: PositiveInt
    roleId: MasterId
    status: Status


class DisableRoleInput(InputModel):
    operation: Literal['DISABLE_ROLE']
    requestId: MasterId
    expectedVersion: PositiveInt
    roleId: MasterId


class SetUserRolesInput(InputModel):
    operation: Literal['SET_USER_ROLES']
    requestId: MasterId
    expectedVersion: PositiveInt
    userId: MasterId
    roleIds: list[MasterId]

    @field_validator('roleIds')
    @classmethod
    def canonical_roles(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Duplicate role IDs are not allowed')
        return sorted(value)


RoleInput = Annotated[ListPermissionsInput | ListUsersInput | ListRolesInput | GetRoleInput |
    GetUserRolesInput | CreateRoleInput | UpdateRoleInput | DisableRoleInput | SetUserRolesInput,
    Field(discriminator='operation')]
ROLE_INPUT = TypeAdapter(RoleInput)


class PermissionOutput(OutputModel):
    code: MasterId
    name: str
    allowedScopeTypes: list[Literal['GLOBAL', 'DEPARTMENT', 'SPACE']]


class UserOutput(OutputModel):
    userId: MasterId
    displayName: str
    departmentId: MasterId | None
    accountStatus: Status


class RoleOutput(OutputModel):
    roleId: MasterId
    name: str
    status: Status
    grants: list[GrantOutput]
    version: PositiveInt


class UserRolesOutput(OutputModel):
    userId: MasterId
    roleIds: list[MasterId]
    version: PositiveInt


class PermissionPage(OutputModel):
    items: list[PermissionOutput]
    nextCursor: Cursor | None


class UserPage(OutputModel):
    items: list[UserOutput]
    nextCursor: Cursor | None


class RolePage(OutputModel):
    items: list[RoleOutput]
    nextCursor: Cursor | None


RoleResult = RoleOutput | UserRolesOutput | PermissionPage | UserPage | RolePage
MUTATION_RESULT = TypeAdapter(RoleOutput | UserRolesOutput)
