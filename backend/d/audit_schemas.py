"""D-S11：严格区分追加、单条查询与筛选分页。"""
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter, model_validator

from backend.d.master_schemas import Cursor, MasterId, nonblank
from backend.shared.clock import format_time, parse_time
from backend.shared.models import InputModel, OutputModel, IsoTime

Source = Literal['M1', 'M2', 'M3', 'D']
Peer = Literal['M1', 'M2', 'M3']
Actor = Literal['USER', 'SYSTEM', 'UNKNOWN']
Result = Literal['SUCCESS', 'REJECTED', 'FAILED', 'UNKNOWN']
Text = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(nonblank)]
EventTime = Annotated[IsoTime, AfterValidator(lambda value: format_time(parse_time(value)))]


class AuditAppendInput(InputModel):
    operation: Literal['APPEND']
    requestId: MasterId
    sourceModule: Peer
    actorType: Actor
    actorId: MasterId | None
    objectType: Text
    businessId: MasterId
    spaceId: MasterId | None
    action: Text
    occurredAt: EventTime
    result: Result
    summary: Text

    @model_validator(mode='after')
    def validate_actor(self):
        if self.actorType == 'UNKNOWN' and self.actorId is not None:
            raise ValueError('UNKNOWN actor requires null actorId')
        if self.actorType == 'USER' and self.actorId is None:
            raise ValueError('USER actor requires userId')
        if self.actorType == 'SYSTEM' and self.actorId != self.sourceModule:
            raise ValueError('SYSTEM actorId must equal uppercase sourceModule')
        return self


class AuditGetInput(InputModel):
    operation: Literal['GET']
    auditId: MasterId = Field(default=None)
    requestId: MasterId = Field(default=None)
    sourceModule: Source = Field(default=None)

    @model_validator(mode='after')
    def validate_identifier(self):
        if (self.auditId is None) == (self.requestId is None):
            raise ValueError('Exactly one auditId or requestId is required')
        return self


class AuditListInput(InputModel):
    operation: Literal['LIST']
    sourceModule: Source = Field(default=None)
    businessId: MasterId = Field(default=None)
    objectType: Text = Field(default=None)
    action: Text = Field(default=None)
    actorUserId: MasterId = Field(default=None)
    spaceId: MasterId = Field(default=None)
    timeFrom: EventTime = Field(default=None)
    timeTo: EventTime = Field(default=None)
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Cursor = Field(default=None)

    @model_validator(mode='after')
    def validate_window(self):
        if self.timeFrom is not None and self.timeTo is not None:
            if parse_time(self.timeFrom) >= parse_time(self.timeTo):
                raise ValueError('timeFrom must be before timeTo')
        return self


AuditInput = Annotated[AuditAppendInput | AuditGetInput | AuditListInput, Field(discriminator='operation')]
AUDIT_INPUT = TypeAdapter(AuditInput)


class AuditEventOutput(OutputModel):
    auditId: MasterId
    requestId: MasterId
    sourceModule: Source
    actorType: Actor
    actorId: MasterId | None
    objectType: Text
    businessId: MasterId
    spaceId: MasterId | None
    action: Text
    occurredAt: IsoTime
    result: Result
    summary: Text
    verifiedBy: Source


class AuditPage(OutputModel):
    items: list[AuditEventOutput]
    nextCursor: Cursor | None


AuditOutput = AuditEventOutput | AuditPage
