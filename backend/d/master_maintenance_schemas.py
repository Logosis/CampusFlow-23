"""D-S07六种严格输入；完整返回复用S06空间/座位格式。"""
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter, field_validator, model_validator

from backend.d.master_schemas import MasterId, SeatOutput, SpaceOutput
from backend.shared.models import InputModel, PositiveInt


def valid_code(value: str) -> str:
    if value != value.strip():
        raise ValueError('seatCode must not have surrounding whitespace')
    return value


SeatCode = Annotated[MasterId, AfterValidator(valid_code)]


class Mutation(InputModel):
    requestId: MasterId


class SpaceValues(InputModel):
    name: Annotated[MasterId, Field(max_length=2000)]
    location: Annotated[MasterId, Field(max_length=2000)]
    isStudyRoom: bool
    ownerKind: Literal['SCHOOL', 'DEPARTMENT']
    departmentId: MasterId | None
    enabled: bool

    @model_validator(mode='after')
    def owner_matches(self):
        if (self.ownerKind == 'SCHOOL') != (self.departmentId is None):
            raise ValueError('ownerKind and departmentId do not match')
        return self


class CreateSpace(Mutation, SpaceValues):
    operation: Literal['CREATE_SPACE']


class UpdateSpace(Mutation, SpaceValues):
    operation: Literal['UPDATE_SPACE']
    spaceId: MasterId
    expectedVersion: PositiveInt


class UnregisterSpace(Mutation):
    operation: Literal['UNREGISTER_SPACE']
    spaceId: MasterId
    expectedVersion: PositiveInt


class SeatValues(InputModel):
    seatCode: SeatCode
    enabled: bool
    socketTags: list[Literal['FIXED', 'RAIL']]

    @field_validator('socketTags')
    @classmethod
    def canonical_tags(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('duplicate socket tags')
        return sorted(value)


class CreateSeat(Mutation, SeatValues):
    operation: Literal['CREATE_SEAT']
    spaceId: MasterId


class UpdateSeat(Mutation, SeatValues):
    operation: Literal['UPDATE_SEAT']
    seatId: MasterId
    expectedVersion: PositiveInt


class UnregisterSeat(Mutation):
    operation: Literal['UNREGISTER_SEAT']
    seatId: MasterId
    expectedVersion: PositiveInt


MaintainMasterInput = Annotated[
    CreateSpace | UpdateSpace | UnregisterSpace | CreateSeat | UpdateSeat | UnregisterSeat,
    Field(discriminator='operation'),
]
MAINTENANCE_INPUT = TypeAdapter(MaintainMasterInput)
MaintainMasterOutput = SpaceOutput | SeatOutput
MAINTENANCE_RESULT = TypeAdapter(MaintainMasterOutput)
