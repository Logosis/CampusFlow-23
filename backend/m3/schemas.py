"""Public v1 shapes, including complete external results."""
from typing import Annotated, Generic, Literal, TypeVar
from pydantic import AfterValidator, Field, model_validator
from backend.shared.models import Id, InputModel, IsoTime, OutputModel, PositiveInt
from backend.shared.clock import parse_time

Status = Literal['WAITING_ASSIGNMENT', 'ASSIGNED', 'REPAIRING', 'WAITING_ACCEPTANCE', 'ACCEPTED', 'CANCELLED']
Stage = Literal['WAITING_ASSIGNMENT', 'ASSIGNED', 'REPAIRING', 'WAITING_ACCEPTANCE']


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError('文本不能为空白')
    return value

Text = Annotated[str, Field(max_length=2000), AfterValidator(nonblank)]
Comment = Annotated[str, Field(max_length=2000)]


class Scope(InputModel):
    type: Literal['GLOBAL', 'DEPARTMENT', 'SPACE']
    id: Id | None

    @model_validator(mode='after')
    def valid_scope(self):
        if (self.type == 'GLOBAL') != (self.id is None):
            raise ValueError('全局范围无编号，其他范围必须有编号')
        return self


class PageInput(InputModel):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Id | None = None


class LocationsInput(PageInput):
    departmentId: Id | None = None
    spaceId: Id | None = None


class ReportInput(InputModel):
    spaceId: Id
    faultLocation: Text
    description: Text
    requestId: Id


class OrderInput(InputModel):
    workOrderId: Id


class VersionInput(OrderInput):
    expectedVersion: PositiveInt
    requestId: Id


class DispatchInput(VersionInput):
    assigneeId: Id
    comment: Comment = ''


class CompletionInput(VersionInput):
    summary: Text


class AcceptanceInput(VersionInput):
    completionId: Id
    decision: Literal['PASS', 'FAIL']
    comment: Comment = ''


class EvaluationInput(OrderInput):
    content: Text
    requestId: Id


class CancelInput(VersionInput):
    reason: Comment = ''


class DispatchTasksInput(PageInput):
    workOrderId: Id | None = None
    spaceId: Id | None = None


class CandidatesInput(PageInput):
    workOrderId: Id


class WorkTasksInput(DispatchTasksInput):
    purpose: Literal['REPAIR', 'ACCEPTANCE', 'EVALUATION']


def validate_interval(start, end):
    if (start is None) != (end is None) or (start and parse_time(start) >= parse_time(end)):
        raise ValueError('起止时间必须成对且开始早于结束')


class OrdersInput(PageInput):
    view: Literal['OWN', 'ASSIGNED', 'MANAGED']
    status: Status | None = None
    startTime: IsoTime | None = None
    endTime: IsoTime | None = None
    spaceId: Id | None = None

    @model_validator(mode='after')
    def interval(self):
        validate_interval(self.startTime, self.endTime)
        return self


class StatsInput(InputModel):
    startTime: IsoTime
    endTime: IsoTime
    spaceId: Id | None = None

    @model_validator(mode='after')
    def interval(self):
        validate_interval(self.startTime, self.endTime)
        return self


class Rule(InputModel):
    ruleId: Id
    stage: Stage
    scope: Scope
    clockStart: Literal['CREATED_AT', 'STAGE_ENTERED_AT']
    timeoutSeconds: PositiveInt
    recipientPermissionCodes: Annotated[list[Id], Field(min_length=1)]
    action: Literal['NOTIFY']

    @model_validator(mode='after')
    def unique_permissions(self):
        if len(self.recipientPermissionCodes) != len(set(self.recipientPermissionCodes)):
            raise ValueError('资格不能重复')
        return self


class ConfigInput(InputModel):
    pass


class UpdateConfigInput(InputModel):
    checkIntervalSeconds: PositiveInt
    rules: list[Rule]
    expectedVersion: PositiveInt
    requestId: Id


class ConfigOutput(OutputModel):
    version: PositiveInt
    checkIntervalSeconds: PositiveInt | None
    rules: list[Rule]


class WorkOrderOutput(OutputModel):
    workOrderId: Id
    reporterId: Id
    spaceId: Id
    faultLocation: Text
    description: Text
    status: Status
    version: PositiveInt
    assigneeId: Id | None
    createdAt: IsoTime
    updatedAt: IsoTime
    stageEnteredAt: IsoTime
    firstRepairStartedAt: IsoTime | None
    acceptedAt: IsoTime | None
    timeoutConfigVersion: PositiveInt | None


class CurrentLimit(OutputModel):
    source: Literal['M2', 'M3']
    businessId: Id
    spaceId: Id
    startTime: IsoTime | None
    endTime: IsoTime | None
    state: Literal['ACTIVE', 'RELEASED']
    sourceVersion: PositiveInt


class LimitResult(OutputModel):
    source: Literal['M2', 'M3']
    businessId: Id
    spaceId: Id | None
    maxProcessedVersion: PositiveInt | None
    currentLimit: CurrentLimit | None
    queriedRequestId: Id | None
    requestStatus: Literal['SUCCESS', 'REJECTED', 'UNKNOWN'] | None
    message: str


class SeatSync(OutputModel):
    requestId: Id
    sourceVersion: PositiveInt
    status: Literal['PENDING', 'SUCCESS', 'REJECTED', 'UNKNOWN']
    result: LimitResult | None
    checkedAt: IsoTime | None
    message: str


class Delivery(OutputModel):
    recipientUserId: Id
    status: Literal['PENDING', 'SENDING', 'SUBMITTED', 'FAILED', 'UNKNOWN']
    attemptCount: Annotated[int, Field(ge=0)]
    lastAttemptAt: IsoTime | None
    reason: str


class Notification(OutputModel):
    notificationId: Id
    requestId: Id
    sourceModule: Literal['M1', 'M2', 'M3']
    businessId: Id
    spaceId: Id | None
    kind: str
    channel: Literal['EMAIL']
    title: str
    body: str
    createdAt: IsoTime
    deliveries: list[Delivery]


class NotificationSync(OutputModel):
    requestId: Id
    status: Literal['PENDING', 'ACCEPTED', 'REJECTED', 'UNKNOWN']
    notification: Notification | None
    message: str


class MutationOutput(OutputModel):
    workOrder: WorkOrderOutput
    recordId: Id | None
    seatSync: SeatSync | None
    notifications: list[NotificationSync]


class Assignment(OutputModel):
    assignmentId: Id
    workOrderId: Id
    dispatcherId: Id
    assigneeId: Id
    comment: str
    assignedAt: IsoTime
    workOrderVersion: PositiveInt


class Completion(OutputModel):
    completionId: Id
    workOrderId: Id
    submittedBy: Id
    summary: str
    submittedAt: IsoTime
    workOrderVersion: PositiveInt


class Acceptance(OutputModel):
    acceptanceId: Id
    workOrderId: Id
    completionId: Id
    acceptedBy: Id
    decision: Literal['PASS', 'FAIL']
    comment: str
    decidedAt: IsoTime
    workOrderVersion: PositiveInt


class Evaluation(OutputModel):
    evaluationId: Id
    workOrderId: Id
    evaluatorId: Id
    content: str
    createdAt: IsoTime


class Escalation(OutputModel):
    escalationId: Id
    workOrderId: Id
    stage: Stage
    cycle: PositiveInt
    configVersion: PositiveInt
    dueAt: IsoTime
    triggeredAt: IsoTime
    action: Literal['NOTIFY']
    recipientUserIds: list[Id]
    notifications: list[NotificationSync]


class Detail(OutputModel):
    workOrder: WorkOrderOutput
    seatSync: SeatSync | None
    assignments: list[Assignment]
    completions: list[Completion]
    acceptances: list[Acceptance]
    evaluations: list[Evaluation]
    escalations: list[Escalation]


class TimelineEvent(OutputModel):
    eventId: Id
    type: Literal['REPORT', 'DISPATCH', 'REPAIR', 'ESCALATE', 'COMPLETE', 'ACCEPT', 'RETURN', 'EVALUATE', 'CANCEL']
    occurredAt: IsoTime
    actorId: Id | None
    summary: str


class DetailOutput(OutputModel):
    detail: Detail
    timeline: list[TimelineEvent]


class Person(OutputModel):
    userId: Id
    displayName: str
    departmentId: Id | None
    accountStatus: Literal['ACTIVE', 'DISABLED']
    eligible: bool | None
    matchedPermissionCodes: list[str]


class Space(OutputModel):
    spaceId: Id
    name: str
    location: str
    isStudyRoom: bool
    ownerKind: Literal['SCHOOL', 'DEPARTMENT']
    departmentId: Id | None
    registered: bool
    enabled: bool
    version: PositiveInt


T = TypeVar('T')


class Page(OutputModel, Generic[T]):
    items: list[T]
    nextCursor: str | None


class StatusCounts(OutputModel):
    WAITING_ASSIGNMENT: int
    ASSIGNED: int
    REPAIRING: int
    WAITING_ACCEPTANCE: int
    ACCEPTED: int
    CANCELLED: int


class StatsOutput(OutputModel):
    startTime: IsoTime
    endTime: IsoTime
    spaceId: Id | None
    checkedAt: IsoTime
    totalCount: int
    statusCounts: StatusCounts
    overdueCount: int
    averageRepairSeconds: float | None
    sampleCount: int
