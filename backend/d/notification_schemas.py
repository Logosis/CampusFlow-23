"""D-S09/S10 通知契约；持久化与接口分步接入，不代表邮件已投递。"""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from backend.d.master_schemas import MasterId, nonblank
from backend.shared.models import InputModel, IsoTime, OutputModel


KINDS_BY_SOURCE = {
    "M1": ("M1_BEFORE_START", "M1_NOT_CHECKED_IN", "M1_AUTO_CANCELLED", "M1_MAINTENANCE_CANCELLED"),
    "M2": ("M2_APPROVAL_PENDING", "M2_APPROVED", "M2_REJECTED", "M2_TIMEOUT_CANCELLED", "M2_CANCELLED"),
    "M3": ("M3_ASSIGNED", "M3_REPAIR_STARTED", "M3_TIMEOUT_ESCALATED", "M3_ACCEPTANCE_PENDING", "M3_ACCEPTANCE_RESULT", "M3_CANCELLED"),
}
NotificationKind = Literal[
    "M1_BEFORE_START", "M1_NOT_CHECKED_IN", "M1_AUTO_CANCELLED", "M1_MAINTENANCE_CANCELLED",
    "M2_APPROVAL_PENDING", "M2_APPROVED", "M2_REJECTED", "M2_TIMEOUT_CANCELLED", "M2_CANCELLED",
    "M3_ASSIGNED", "M3_REPAIR_STARTED", "M3_TIMEOUT_ESCALATED", "M3_ACCEPTANCE_PENDING", "M3_ACCEPTANCE_RESULT", "M3_CANCELLED",
]
NoticeText = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(nonblank)]


class SendNotificationInput(InputModel):
    requestId: MasterId
    sourceModule: Literal["M1", "M2", "M3"]
    businessId: MasterId
    spaceId: MasterId | None
    recipientUserIds: Annotated[list[MasterId], Field(min_length=1)]
    kind: NotificationKind
    title: NoticeText
    body: NoticeText

    @model_validator(mode="after")
    def validate_notification(self):
        if self.kind not in KINDS_BY_SOURCE[self.sourceModule]:
            raise ValueError("notification kind must match sourceModule")
        if len(set(self.recipientUserIds)) != len(self.recipientUserIds):
            raise ValueError("recipientUserIds must be unique")
        if "\r" in self.title or "\n" in self.title:
            raise ValueError("title must be a single line")
        return self


class GetNotificationInput(InputModel):
    notificationId: MasterId = Field(default=None)
    requestId: MasterId = Field(default=None)

    @model_validator(mode="after")
    def validate_identifier(self):
        if (self.notificationId is None) == (self.requestId is None):
            raise ValueError("provide exactly one notificationId or requestId")
        return self


class DeliveryOutput(OutputModel):
    recipientUserId: MasterId
    status: Literal["PENDING", "SENDING", "SUBMITTED", "FAILED", "UNKNOWN"]
    attemptCount: Annotated[int, Field(ge=0)]
    lastAttemptAt: IsoTime | None
    reason: str


class NotificationOutput(OutputModel):
    notificationId: MasterId
    requestId: MasterId
    sourceModule: Literal["M1", "M2", "M3"]
    businessId: MasterId
    spaceId: MasterId | None
    kind: NotificationKind
    channel: Literal["EMAIL"]
    title: NoticeText
    body: NoticeText
    createdAt: IsoTime
    deliveries: Annotated[list[DeliveryOutput], Field(min_length=1)]
