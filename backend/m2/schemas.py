"""M2 公开输入与输出格式。

字段名、大小写、可省与可空按 docs/planning/R1/Module_M2/M2.md；输入严格拒绝未知字段。
这里只描述对外契约，内部表结构在 models.py。
"""
from typing import Literal

from pydantic import AfterValidator, Field
from typing_extensions import Annotated

from backend.shared.models import Id, InputModel, IsoTime, OutputModel, PositiveInt

# ---------------------------------------------------------------- 公共子结构
ScopeType = Literal["GLOBAL", "DEPARTMENT", "SPACE"]
ApplicationStatus = Literal["PENDING", "APPROVED", "REJECTED", "TIMEOUT_CANCELLED", "CANCELLED"]
Decision = Literal["APPROVE", "REJECT"]
ActionName = Literal[
    "SUBMIT", "APPROVE", "READ", "CONFIGURE", "CANCEL", "MANAGED_READ"
]
ClockStart = Literal["SUBMISSION", "CURRENT_LEVEL"]
SyncStatus = Literal["PENDING", "SUCCESS", "REJECTED", "UNKNOWN"]
NotificationStatus = Literal["PENDING", "ACCEPTED", "REJECTED", "UNKNOWN"]
ViewName = Literal["OWN", "HANDLED", "MANAGED"]
BlockedReason = Literal["BASE", "OPENING", "M1", "M2", "M3", "NO_SEATS"]


def _positive(value: int) -> int:
    if isinstance(value, bool) or value <= 0:
        raise ValueError("必须为正整数")
    return value


LimitValue = Annotated[int, Field(ge=1, le=100)]
Positive = Annotated[int, AfterValidator(_positive)]


class Scope(OutputModel):
    type: ScopeType
    id: str | None


class TargetScope(InputModel):
    type: ScopeType
    id: str | None = None


class SpaceRecord(OutputModel):
    """与 D-S06 GET_SPACE 一致。"""

    spaceId: str
    name: str
    location: str
    isStudyRoom: bool
    ownerKind: Literal["SCHOOL", "DEPARTMENT"]
    departmentId: str | None
    registered: bool
    enabled: bool
    version: int


class Availability(OutputModel):
    """与 M1-S09 get_room_availability 输出一致。"""

    spaceId: str
    startTime: str
    endTime: str
    canOccupy: bool
    blockedBy: list[BlockedReason]
    reasons: list[str]
    checkedAt: str


class ApplicationOut(OutputModel):
    """S02.application 格式。"""

    applicationId: str
    applicantId: str
    spaceId: str
    startTime: str
    endTime: str
    purpose: str
    status: ApplicationStatus
    currentLevel: int | None
    version: int
    configVersion: int
    submittedAt: str
    currentLevelStartedAt: str | None
    expiresAt: str | None
    terminatedAt: str | None
    terminationReason: str


class ApprovalOut(OutputModel):
    """S05.approvals 单项。"""

    approvalId: str
    applicationId: str
    level: int
    approverId: str
    decision: Decision
    comment: str
    decidedAt: str
    applicationVersion: int


class RoomLimitOut(OutputModel):
    """与 M1-S11 get_room_limit 输出一致；用于 seatSync.result。"""

    source: Literal["M2", "M3"]
    businessId: str
    spaceId: str | None
    maxProcessedVersion: int | None
    currentLimit: dict | None
    queriedRequestId: str | None
    requestStatus: Literal["SUCCESS", "REJECTED", "UNKNOWN"] | None
    message: str


class SeatSyncOut(OutputModel):
    requestId: str
    sourceVersion: int
    status: SyncStatus
    result: RoomLimitOut | None
    checkedAt: str | None
    message: str


class DeliveriesOut(OutputModel):
    recipientUserId: str
    status: Literal["PENDING", "SENDING", "SUBMITTED", "FAILED", "UNKNOWN"]
    attemptCount: int
    lastAttemptAt: str | None
    reason: str


class NotificationOut(OutputModel):
    """与 D-S09 输出一致。"""

    notificationId: str
    requestId: str
    sourceModule: str
    businessId: str
    spaceId: str | None
    kind: str
    channel: str
    title: str
    body: str
    createdAt: str
    deliveries: list[DeliveriesOut]


class NotificationView(OutputModel):
    requestId: str
    status: NotificationStatus
    notification: NotificationOut | None
    message: str


class ApplicationView(OutputModel):
    """S05 输出；S02/S04/S06/S07 复用同一单项格式。"""

    application: ApplicationOut
    approvals: list[ApprovalOut]
    seatSync: SeatSyncOut | None
    notifications: list[NotificationView]


class PagedApplications(OutputModel):
    items: list[ApplicationView]
    nextCursor: str | None


class AllowedAction(OutputModel):
    operation: ActionName
    allowed: bool
    allowedScopes: list[Scope]
    reason: str


class AllowedActionsOut(OutputModel):
    actions: list[AllowedAction]


class ClassroomOption(OutputModel):
    space: SpaceRecord
    availability: Availability | None


class PagedClassroomOptions(OutputModel):
    items: list[ClassroomOption]
    nextCursor: str | None


class MaintenanceImpact(OutputModel):
    applicationId: str
    affectedByMaintenance: bool
    availability: Availability


class ApprovalLevelConfig(InputModel):
    level: int
    approverId: Id


class ApprovalLevelView(OutputModel):
    level: int
    approverId: str


class ApprovalConfigOut(OutputModel):
    version: int
    applicationScopes: list[Scope]
    approvalLevels: list[ApprovalLevelView]
    clockStart: ClockStart | None
    timeoutSeconds: int | None
    checkIntervalSeconds: int | None


# ---------------------------------------------------------------- 各服务输入
class GetAllowedActionsInput(InputModel):
    spaceId: Id | None = None


class SubmitApplicationInput(InputModel):
    spaceId: Id
    startTime: IsoTime
    endTime: IsoTime
    purpose: str
    requestId: Id


class GetApprovalTasksInput(InputModel):
    applicationId: Id | None = None
    spaceId: Id | None = None
    limit: LimitValue | None = None
    cursor: Id | None = None


class SubmitApprovalInput(InputModel):
    applicationId: Id
    level: int
    decision: Decision
    comment: str | None = None
    expectedVersion: PositiveInt
    requestId: Id


class GetApplicationInput(InputModel):
    applicationId: Id


class GetApplicationsInput(InputModel):
    view: ViewName
    status: ApplicationStatus | None = None
    startTime: IsoTime | None = None
    endTime: IsoTime | None = None
    spaceId: Id | None = None
    applicationId: Id | None = None
    limit: LimitValue | None = None
    cursor: Id | None = None


class CancelApplicationInput(InputModel):
    applicationId: Id
    reason: str | None = None
    expectedVersion: PositiveInt
    requestId: Id


class GetApprovalConfigInput(InputModel):
    pass


class UpdateApprovalConfigInput(InputModel):
    applicationScopes: list[Scope]
    approvalLevels: list[ApprovalLevelView]
    clockStart: ClockStart
    timeoutSeconds: Positive
    checkIntervalSeconds: Positive
    expectedVersion: PositiveInt
    requestId: Id


class GetMaintenanceImpactInput(InputModel):
    applicationId: Id


class GetClassroomOptionsInput(InputModel):
    departmentId: Id | None = None
    startTime: IsoTime | None = None
    endTime: IsoTime | None = None
    limit: LimitValue | None = None
    cursor: Id | None = None
