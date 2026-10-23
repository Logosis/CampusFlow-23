"""D-S06：按 operation 区分输入，拒绝错用筛选字段和显式 null。"""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter

from backend.shared.models import InputModel, OutputModel, PositiveInt


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


MasterId = Annotated[str, Field(min_length=1), AfterValidator(nonblank)]
Cursor = Annotated[str, Field(min_length=1, max_length=8192), AfterValidator(nonblank)]


class MasterPageInput(InputModel):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Cursor = Field(default=None)


class ListDepartmentsInput(MasterPageInput):
    operation: Literal["LIST_DEPARTMENTS"]


class ListSpacesInput(MasterPageInput):
    operation: Literal["LIST_SPACES"]
    departmentId: MasterId = Field(default=None)
    ownerKind: Literal["SCHOOL", "DEPARTMENT"] = Field(default=None)
    isStudyRoom: bool = Field(default=None)
    enabled: bool = Field(default=None)
    includeUnregistered: bool = False


class GetSpaceInput(InputModel):
    operation: Literal["GET_SPACE"]
    spaceId: MasterId
    includeUnregistered: bool = False


class ListSeatsInput(MasterPageInput):
    operation: Literal["LIST_SEATS"]
    spaceId: MasterId = Field(default=None)
    departmentId: MasterId = Field(default=None)
    enabled: bool = Field(default=None)
    socketTag: Literal["FIXED", "RAIL"] = Field(default=None)
    includeUnregistered: bool = False


class GetSeatInput(InputModel):
    operation: Literal["GET_SEAT"]
    seatId: MasterId
    includeUnregistered: bool = False


QueryMasterInput = Annotated[
    ListDepartmentsInput | ListSpacesInput | GetSpaceInput | ListSeatsInput | GetSeatInput,
    Field(discriminator="operation"),
]
MASTER_INPUT = TypeAdapter(QueryMasterInput)


class DepartmentOutput(OutputModel):
    departmentId: MasterId
    name: str


class SpaceOutput(OutputModel):
    spaceId: MasterId
    name: str
    location: str
    isStudyRoom: bool
    ownerKind: Literal["SCHOOL", "DEPARTMENT"]
    departmentId: MasterId | None
    registered: bool
    enabled: bool
    version: PositiveInt


class SeatOutput(OutputModel):
    seatId: MasterId
    seatCode: MasterId
    spaceId: MasterId
    registered: bool
    enabled: bool
    socketTags: list[Literal["FIXED", "RAIL"]]
    version: PositiveInt


class DepartmentPage(OutputModel):
    items: list[DepartmentOutput]
    nextCursor: Cursor | None


class SpacePage(OutputModel):
    items: list[SpaceOutput]
    nextCursor: Cursor | None


class SeatPage(OutputModel):
    items: list[SeatOutput]
    nextCursor: Cursor | None


QueryMasterOutput = DepartmentPage | SpacePage | SeatPage | SpaceOutput | SeatOutput
